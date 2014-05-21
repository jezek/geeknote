#!/usr/bin/env python
# -*- coding: utf-8 -*-

import traceback
import time
import sys
import os
import re

import thrift.protocol.TBinaryProtocol as TBinaryProtocol
import thrift.transport.THttpClient as THttpClient

import evernote.edam.userstore.constants as UserStoreConstants
import evernote.edam.notestore.NoteStore as NoteStore
import evernote.edam.error.ttypes as Errors
import evernote.edam.type.ttypes as Types

import config
import mimetypes
import hashlib
import tools
import out
from editor import Editor, EditorThread
from gclient import GUserStore as UserStore
from argparser import argparser
from oauth import GeekNoteAuth
from storage import Storage
from log import logging


def GeekNoneDBConnectOnly(func):
    """ operator to disable evernote connection
    or create instance of GeekNote """
    def wrapper(*args, **kwargs):
        GeekNote.skipInitConnection = True
        return func(*args, **kwargs)
    return wrapper


def make_resource(filename):
    try:
        mtype = mimetypes.guess_type(filename)[0]

        if mtype and mtype.split('/')[0] == "text":
            rmode = "r"
        else:
            rmode = "rb"

        with open(filename, rmode) as f:
            """ file exists """
            resource = Types.Resource()
            resource.data = Types.Data()

            data = f.read()
            md5 = hashlib.md5()
            md5.update(data)

            resource.data.bodyHash = md5.hexdigest()
            resource.data.body = data
            resource.data.size = len(data)
            resource.mime = mtype
            resource.attributes = Types.ResourceAttributes()
            resource.attributes.fileName = os.path.basename(filename)
            return resource
    except IOError:
        msg = "The file '%s' does not exist." % filename
        out.failureMessage(msg)
        raise IOError(msg)


class GeekNote(object):

    userStoreUri = config.USER_STORE_URI
    consumerKey = config.CONSUMER_KEY
    consumerSecret = config.CONSUMER_SECRET
    authToken = None
    userStore = None
    noteStore = None
    storage = None
    skipInitConnection = False

    def __init__(self, skipInitConnection=False):
        if skipInitConnection:
            self.skipInitConnection = True

        self.getStorage()

        if self.skipInitConnection is True:
            return

        self.getUserStore()

        if not self.checkAuth():
            self.auth()

    def EdamException(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception, e:
                logging.error("Error: %s : %s", func.__name__, str(e))

                if not hasattr(e, 'errorCode'):
                    out.failureMessage("Sorry, operation has failed!!!.")
                    tools.exit()

                errorCode = int(e.errorCode)

                # auth-token error, re-auth
                if errorCode == 9:
                    storage = Storage()
                    storage.removeUser()
                    GeekNote()
                    return func(*args, **kwargs)

                elif errorCode == 3:
                    out.failureMessage("Sorry, you do not have permissions "
                                       "to do this operation.")

                else:
                    return False

                tools.exit()

        return wrapper

    def getStorage(self):
        if GeekNote.storage:
            return GeekNote.storage

        GeekNote.storage = Storage()
        return GeekNote.storage

    def getUserStore(self):
        if GeekNote.userStore:
            return GeekNote.userStore

        userStoreHttpClient = THttpClient.THttpClient(self.userStoreUri)
        userStoreProtocol = TBinaryProtocol.TBinaryProtocol(userStoreHttpClient)
        GeekNote.userStore = UserStore.Client(userStoreProtocol)

        self.checkVersion()

        return GeekNote.userStore

    def getNoteStore(self):
        if GeekNote.noteStore:
            return GeekNote.noteStore

        noteStoreUrl = self.getUserStore().getNoteStoreUrl(self.authToken)
        noteStoreHttpClient = THttpClient.THttpClient(noteStoreUrl)
        noteStoreProtocol = TBinaryProtocol.TBinaryProtocol(noteStoreHttpClient)
        GeekNote.noteStore = NoteStore.Client(noteStoreProtocol)

        return GeekNote.noteStore

    def checkVersion(self):
        versionOK = self.getUserStore().checkVersion("Python EDAMTest",
                                       UserStoreConstants.EDAM_VERSION_MAJOR,
                                       UserStoreConstants.EDAM_VERSION_MINOR)
        if not versionOK:
            logging.error("Old EDAM version")
            return tools.exit()

    def checkAuth(self):
        self.authToken = self.getStorage().getUserToken()
        logging.debug("oAuth token : %s", self.authToken)
        if self.authToken:
            return True
        return False

    def auth(self):
        GNA = GeekNoteAuth()
        self.authToken = GNA.getToken()
        userInfo = self.getUserInfo()
        if not isinstance(userInfo, object):
            logging.error("User info not get")
            return False

        self.getStorage().createUser(self.authToken, userInfo)
        return True

    def getUserInfo(self):
        return self.getUserStore().getUser(self.authToken)

    def removeUser(self):
        return self.getStorage().removeUser()

    @EdamException
    def findNotes(self, keywords, count, createOrder=False, offset=0):
        """ WORK WITH NOTES """
        noteFilter = NoteStore.NoteFilter(order=Types.NoteSortOrder.RELEVANCE)
        if createOrder:
            noteFilter.order = Types.NoteSortOrder.CREATED

        if keywords:
            noteFilter.words = keywords
        return self.getNoteStore().findNotes(self.authToken, noteFilter, offset, count)

    @EdamException
    def loadNoteContent(self, note):
        """ modify Note object """
        if not isinstance(note, object):
            raise Exception("Note content must be an "
                            "instance of Note, '%s' given." % type(note))

        note.content = self.getNoteStore().getNoteContent(self.authToken, note.guid)
        # fill the tags in
        if note.tagGuids and not note.tagNames:
          note.tagNames = [];
          for guid in note.tagGuids:
            tag = self.getNoteStore().getTag(self.authToken,guid)
            note.tagNames.append(tag.name)

    @EdamException
    def createNote(self, title, content, tags=None, notebook=None, created=None, resources=None, reminder=None):
        note = Types.Note()
        note.title = title
        note.content = content
        note.created = created

        if tags:
            note.tagNames = tags

        if notebook:
            note.notebookGuid = notebook

        if resources:
            """ make EverNote API resources """
            note.resources = map(make_resource, resources)

            """ add to content """
            resource_nodes = ""

            for resource in note.resources:
                resource_nodes += '<en-media type="%s" hash="%s" />' % (resource.mime, resource.data.bodyHash)

            note.content = note.content.replace("</en-note>", resource_nodes + "</en-note>")

        # Allow creating a completed reminder (for task tracking purposes),
        # skip reminder creation steps if we have a DELETE
        if reminder and reminder != config.REMINDER_DELETE:
            now = int(round(time.time() * 1000))
            note.attributes = Types.NoteAttributes()
            if reminder == config.REMINDER_NONE:
                note.attributes.reminderOrder = now
            elif reminder == config.REMINDER_DONE:
                note.attributes.reminderOrder = now
                note.attributes.reminderDoneTime = now
            else:  # we have an actual reminder time stamp
                if reminder > now:  # future reminder only
                    note.attributes.reminderOrder = now
                    note.attributes.reminderTime = reminder
                else:
                    out.failureMessage("Sorry, reminder must be in the future.")
                    tools.exit()

        logging.debug("New note : %s", note)

        return self.getNoteStore().createNote(self.authToken, note)

    @EdamException
    def updateNote(self, guid, title=None, content=None,
                   tags=None, notebook=None, resources=None, reminder=None):
        note = Types.Note()
        note.guid = guid
        if title:
            note.title = title

        if content:
            note.content = content

        if tags:
            note.tagNames = tags

        if notebook:
            note.notebookGuid = notebook

        if resources:
            """ make EverNote API resources """
            note.resources = map(make_resource, resources)

            """ add to content """
            resource_nodes = ""

            for resource in note.resources:
                resource_nodes += '<en-media type="%s" hash="%s" />' % (resource.mime, resource.data.bodyHash)

            if not note.content:
                note.content = '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd"><en-note></en-note>'
            note.content = note.content.replace("</en-note>", resource_nodes + "</en-note>")

        if reminder:
            now = int(round(time.time() * 1000))
            if not note.attributes:  # in case no attributes available
                note.attributes = Types.NoteAttributes()
            if reminder == config.REMINDER_NONE:
                note.attributes.reminderDoneTime = None
                note.attributes.reminderTime = None
                if not note.attributes.reminderOrder:  # new reminder
                    note.attributes.reminderOrder = now
            elif reminder == config.REMINDER_DONE:
                note.attributes.reminderDoneTime = now
                if not note.attributes.reminderOrder: # catch adding DONE to non-reminder
                    note.attributes.reminderOrder = now
                    note.attributes.reminderTime = None
            elif reminder == config.REMINDER_DELETE:
                note.attributes.reminderOrder = None
                note.attributes.reminderTime = None
                note.attributes.reminderDoneTime = None
            else:  # we have an actual reminder timestamp
                if reminder > now:  # future reminder only
                    note.attributes.reminderTime = reminder
                    note.attributes.reminderDoneTime = None
                    if not note.attributes.reminderOrder:  # catch adding time to non-reminder
                        note.attributes.reminderOrder = now
                else:
                    out.failureMessage("Sorry, reminder must be in the future.")
                    tools.exit()
        logging.debug("Update note : %s", note)

        self.getNoteStore().updateNote(self.authToken, note)
        return True

    @EdamException
    def removeNote(self, guid):
        logging.debug("Delete note with guid: %s", guid)

        self.getNoteStore().deleteNote(self.authToken, guid)
        return True

    @EdamException
    def findNotebooks(self):
        """ WORK WITH NOTEBOOKS """
        return self.getNoteStore().listNotebooks(self.authToken)

    @EdamException
    def createNotebook(self, name):
        notebook = Types.Notebook()
        notebook.name = name

        logging.debug("New notebook : %s", notebook)

        result = self.getNoteStore().createNotebook(self.authToken, notebook)
        return result

    @EdamException
    def updateNotebook(self, guid, name):
        notebook = Types.Notebook()
        notebook.name = name
        notebook.guid = guid

        logging.debug("Update notebook : %s", notebook)

        self.getNoteStore().updateNotebook(self.authToken, notebook)
        return True

    @EdamException
    def removeNotebook(self, guid):
        logging.debug("Delete notebook : %s", guid)

        self.getNoteStore().expungeNotebook(self.authToken, guid)
        return True

    @EdamException
    def findTags(self):
        """ WORK WITH TAGS """
        return self.getNoteStore().listTags(self.authToken)

    @EdamException
    def createTag(self, name):
        tag = Types.Tag()
        tag.name = name

        logging.debug("New tag : %s", tag)

        result = self.getNoteStore().createTag(self.authToken, tag)
        return result

    @EdamException
    def updateTag(self, guid, name):
        tag = Types.Tag()
        tag.name = name
        tag.guid = guid

        logging.debug("Update tag : %s", tag)

        self.getNoteStore().updateTag(self.authToken, tag)
        return True

    @EdamException
    def removeTag(self, guid):
        logging.debug("Delete tag : %s", guid)

        self.getNoteStore().expungeTag(self.authToken, guid)
        return True


class GeekNoteConnector(object):
    evernote = None
    storage = None

    def connectToEvertone(self):
        out.preloader.setMessage("Connect to Evernote...")
        self.evernote = GeekNote()

    def getEvernote(self):
        if self.evernote:
            return self.evernote

        self.connectToEvertone()
        return self.evernote

    def getStorage(self):
        if self.storage:
            return self.storage

        self.storage = self.getEvernote().getStorage()
        return self.storage

class User(GeekNoteConnector):
    """ Work with auth User """

    @GeekNoneDBConnectOnly
    def user(self, full=None):
        if not self.getEvernote().checkAuth():
            out.failureMessage("You not logged in.")
            return tools.exit()

        if full:
            info = self.getEvernote().getUserInfo()
        else:
            info = self.getStorage().getUserInfo()
        out.showUser(info, full)

    @GeekNoneDBConnectOnly
    def login(self):
        if self.getEvernote().checkAuth():
            out.successMessage("You have already logged in.")
            return tools.exit()

        if self.getEvernote().auth():
            out.successMessage("You have successfully logged in.")
        else:
            out.failureMessage("Login error.")
            return tools.exit()

    @GeekNoneDBConnectOnly
    def logout(self, force=None):
        if not self.getEvernote().checkAuth():
            out.successMessage("You have already logged out.")
            return tools.exit()

        if not force and not out.confirm('Are you sure you want to logout?'):
            return tools.exit()

        result = self.getEvernote().removeUser()
        if result:
            out.successMessage("You have successfully logged out.")
        else:
            out.failureMessage("Logout error.")
            return tools.exit()

    @GeekNoneDBConnectOnly
    def settings(self, editor=None):
        storage = self.getStorage()
        if editor:
            if editor == '#GET#':
                editor = storage.getUserprop('editor')
                if not editor:
                    editor = config.DEF_WIN_EDITOR if sys.platform == 'win32' else config.DEF_UNIX_EDITOR
                out.successMessage("Current editor is: %s" % editor)
            else:
                storage.setUserprop('editor', editor)
                out.successMessage("Changes have been saved.")
        else:
            settings = ('Geeknote',
                        '*' * 30,
                        'Version: %s' % config.VERSION,
                        'App dir: %s' % config.APP_DIR,
                        'Error log: %s' % config.ERROR_LOG,
                        'Current editor: %s' % storage.getUserprop('editor'))

            user_settings = storage.getUserprops()

            if user_settings:
                user = user_settings[1]['info']
                settings += ('*' * 30,
                             'Username: %s' % user.username,
                             'Id: %s' % user.id,
                             'Email: %s' % user.email)

            out.printLine('\n'.join(settings))


class Tags(GeekNoteConnector):
    """ Work with auth Notebooks """

    def list(self):
        result = self.getEvernote().findTags()
        out.printList(result)

    def create(self, title):
        self.connectToEvertone()
        out.preloader.setMessage("Creating tag...")
        result = self.getEvernote().createTag(name=title)

        if result:
            out.successMessage("Tag has been successfully created.")
        else:
            out.failureMessage("Error while the process of creating the tag.")
            return tools.exit()

        return result

    def edit(self, tagname, title):
        tag = self._searchTag(tagname)

        out.preloader.setMessage("Updating tag...")
        result = self.getEvernote().updateTag(guid=tag.guid, name=title)

        if result:
            out.successMessage("Tag has been successfully updated.")
        else:
            out.failureMessage("Error while the updating the tag.")
            return tools.exit()

    def remove(self, tagname, force=None):
        tag = self._searchTag(tagname)

        if not force and not out.confirm('Are you sure you want to '
                                         'delete this tag: "%s"?' % tag.name):
            return tools.exit()

        out.preloader.setMessage("Deleting tag...")
        result = self.getEvernote().removeTag(guid=tag.guid)

        if result:
            out.successMessage("Tag has been successfully removed.")
        else:
            out.failureMessage("Error while removing the tag.")

    def _searchTag(self, tag):
        result = self.getEvernote().findTags()
        tag = [item for item in result if item.name == tag]

        if tag:
            tag = tag[0]
        else:
            tag = out.SelectSearchResult(result)

        logging.debug("Selected tag: %s" % str(tag))
        return tag


class Notebooks(GeekNoteConnector):
    """ Work with auth Notebooks """

    def list(self):
        result = self.getEvernote().findNotebooks()
        out.printList(result)

    def create(self, title):
        self.connectToEvertone()
        out.preloader.setMessage("Creating notebook...")
        result = self.getEvernote().createNotebook(name=title)

        if result:
            out.successMessage("Notebook has been successfully created.")
        else:
            out.failureMessage("Error while the process "
                               "of creating the notebook.")
            return tools.exit()

        return result

    def edit(self, notebook, title):
        notebook = self._searchNotebook(notebook)

        out.preloader.setMessage("Updating notebook...")
        result = self.getEvernote().updateNotebook(guid=notebook.guid,
                                                   name=title)

        if result:
            out.successMessage("Notebook has been successfully updated.")
        else:
            out.failureMessage("Error while the updating the notebook.")
            return tools.exit()

    def remove(self, notebook, force=None):
        notebook = self._searchNotebook(notebook)

        if not force and not out.confirm('Are you sure you want to delete'
                                         ' this notebook: "%s"?' % notebook.name):
            return tools.exit()

        out.preloader.setMessage("Deleting notebook...")
        result = self.getEvernote().removeNotebook(guid=notebook.guid)

        if result:
            out.successMessage("Notebook has been successfully removed.")
        else:
            out.failureMessage("Error while removing the notebook.")

    def _searchNotebook(self, notebook):

        result = self.getEvernote().findNotebooks()
        notebook = [item for item in result if item.name == notebook]

        if notebook:
            notebook = notebook[0]
        else:
            notebook = out.SelectSearchResult(result)

        logging.debug("Selected notebook: %s" % str(notebook))
        return notebook

    def getNoteGUID(self, notebook):
        if len(notebook) == 36 and notebook.find("-") == 4:
            return notebook

        result = self.getEvernote().findNotebooks()
        notebook = [item for item in result if item.name == notebook]
        if notebook:
            return notebook[0].guid
        else:
            return None


class Notes(GeekNoteConnector):
    """ Work with Notes """

    findExactOnUpdate = False
    selectFirstOnUpdate = False

    def __init__(self, findExactOnUpdate=False, selectFirstOnUpdate=False):
        self.findExactOnUpdate = bool(findExactOnUpdate)
        self.selectFirstOnUpdate = bool(selectFirstOnUpdate)

    def _editWithEditorInThread(self, inputData, note=None):
        if note:
            self.getEvernote().loadNoteContent(note)
            editor = Editor(note.content)
        else:
            editor = Editor('')
        thread = EditorThread(editor)
        thread.start()

        result = True
        prevChecksum = editor.getTempfileChecksum()
        while True:
            if prevChecksum != editor.getTempfileChecksum() and result:
                newContent = open(editor.tempfile, 'r').read()
                inputData['content'] = Editor.textToENML(newContent)
                if not note:
                    result = self.getEvernote().createNote(**inputData)
                    # TODO: log error if result is False or None
                    if result:
                        note = result
                    else:
                        result = False
                else:
                    result = bool(self.getEvernote().updateNote(guid=note.guid, **inputData))
                    # TODO: log error if result is False

                if result:
                    prevChecksum = editor.getTempfileChecksum()

            if not thread.isAlive():
                # check if thread is alive here before sleep to avoid losing data saved during this 5 secs
                break
            time.sleep(5)
        return result

    def create(self, title, content=None, tags=None, notebook=None, resource=None, reminder=None):

        self.connectToEvertone()

        # Optional Content.
        content = content or " "

        inputData = self._parseInput(title, content, tags, notebook, resource, reminder=reminder)

        if inputData['content'] == config.EDITOR_OPEN:
            result = self._editWithEditorInThread(inputData)
        else:
            out.preloader.setMessage("Creating note...")
            result = bool(self.getEvernote().createNote(**inputData))

        if result:
            out.successMessage("Note has been successfully created.")
        else:
            out.failureMessage("Error while creating the note.")

    def edit(self, note, title=None, content=None, tags=None, notebook=None, resource=None, reminder=None):

        self.connectToEvertone()
        note = self._searchNote(note)

        inputData = self._parseInput(title, content, tags, notebook, resource, note, reminder=reminder)

        if inputData['content'] == config.EDITOR_OPEN:
            result = self._editWithEditorInThread(inputData, note)
        else:
            out.preloader.setMessage("Saving note...")
            result = bool(self.getEvernote().updateNote(guid=note.guid, **inputData))

        if result:
            out.successMessage("Note has been successfully saved.")
        else:
            out.failureMessage("Error while saving the note.")

    def remove(self, note, force=None):

        self.connectToEvertone()
        note = self._searchNote(note)

        if not force and not out.confirm('Are you sure you want to '
                                         'delete this note: "%s"?' % note.title):
            return tools.exit()

        out.preloader.setMessage("Deleting note...")
        result = self.getEvernote().removeNote(note.guid)

        if result:
            out.successMessage("Note has been successful deleted.")
        else:
            out.failureMessage("Error while deleting the note.")

    def show(self, note):

        self.connectToEvertone()

        note = self._searchNote(note)

        out.preloader.setMessage("Loading note...")
        self.getEvernote().loadNoteContent(note)

        out.showNote(note)

    def _parseInput(self, title=None, content=None, tags=None, notebook=None, resources=None, note=None, reminder=None):
        result = {
            "title": title,
            "content": content,
            "tags": tags,
            "notebook": notebook,
            "resources": resources if resources else [],
            "reminder": reminder,
        }
        result = tools.strip(result)

        # if get note without params
        if note and title is None and content is None and tags is None and notebook is None:
            content = config.EDITOR_OPEN

        if title is None and note:
            result['title'] = note.title

        if content:
            if content != config.EDITOR_OPEN:
                if isinstance(content, str) and os.path.isfile(content):
                    logging.debug("Load content from the file")
                    content = open(content, "r").read()

                logging.debug("Convert content")
                content = Editor.textToENML(content)
            result['content'] = content

        if tags:
            result['tags'] = tools.strip(tags.split(','))

        if notebook:
            notepadGuid = Notebooks().getNoteGUID(notebook)
            if notepadGuid is None:
                newNotepad = Notebooks().create(notebook)
                notepadGuid = newNotepad.guid

            result['notebook'] = notepadGuid
            logging.debug("Search notebook")

        if reminder:
            then = config.REMINDER_SHORTCUTS.get(reminder)
            if then:
                now = int(round(time.time() * 1000))
                result['reminder'] = now + then
            elif reminder not in [config.REMINDER_NONE, config.REMINDER_DONE, config.REMINDER_DELETE]:
                reminder = tools.strip(reminder.split('-'))
                try:
                    dateStruct = time.strptime(reminder[0] + " "  + reminder[1] + ":00", config.DEF_DATE_AND_TIME_FORMAT)
                    reminderTime = int(round(time.mktime(dateStruct) * 1000))
                    result['reminder'] = reminderTime
                except (ValueError, IndexError), e:
                    out.failureMessage('Incorrect date format in --reminder attribute. '
                                       'Format: %s' % time.strftime(config.DEF_DATE_FORMAT, time.strptime('199912311422', "%Y%m%d%H%M")))
                    return tools.exit()

        return result

    def _searchNote(self, note):
        note = tools.strip(note)

        # load search result
        result = self.getStorage().getSearch()
        if result and tools.checkIsInt(note) and 1 <= int(note) <= len(result.notes):
            note = result.notes[int(note) - 1]

        else:
            request = self._createSearchRequest(search=note)

            logging.debug("Search notes: %s" % request)
            result = self.getEvernote().findNotes(request, 20)

            logging.debug("Search notes result: %s" % str(result))
            if result.totalNotes == 0:
                out.successMessage("Notes have not been found.")
                return tools.exit()

            elif result.totalNotes == 1 or self.selectFirstOnUpdate:
                note = result.notes[0]

            else:
                logging.debug("Choose notes: %s" % str(result.notes))
                note = out.SelectSearchResult(result.notes)

        logging.debug("Selected note: %s" % str(note))
        return note

    def find(self, search=None, tags=None, notebooks=None,
             date=None, exact_entry=None, content_search=None,
             with_url=None, count=None, ignore_completed=None, reminders_only=None,):

        request = self._createSearchRequest(search, tags, notebooks,
                                            date, exact_entry,
                                            content_search,
                                            ignore_completed, reminders_only)

        if not count:
            count = 20
        else:
            count = int(count)

        logging.debug("Search count: %s", count)

        createFilter = True if search == "*" else False
        result = self.getEvernote().findNotes(request, count, createFilter)

        # Reduces the count by the amount of notes already retrieved
        update_count = lambda c: max(c - len(result.notes), 0)

        count = update_count(count)

        # Evernote api will only return so many notes in one go. Checks for more
        # notes to come whilst obeying count rules
        while ((result.totalNotes != len(result.notes)) and count != 0):
            offset = len(result.notes)
            result.notes += self.getEvernote().findNotes(request, count,
                    createFilter, offset).notes
            count = update_count(count)

        if result.totalNotes == 0:
            out.successMessage("Notes have not been found.")

        # save search result
        # print result
        self.getStorage().setSearch(result)

        out.SearchResult(result.notes, request, showUrl=with_url)

    def _createSearchRequest(self, search=None, tags=None,
                             notebooks=None, date=None,
                             exact_entry=None, content_search=None,
                             ignore_completed=None, reminders_only=None):

        request = ""

        def _formatExpression(label, value):
            """Create an expression like label:value, attending to negation and quotes """
            expression = ""

            # if negated, prepend that to the expression before labe, not value
            if value.startswith('-'):
                expression += '-'
                value = value[1:]
            value = tools.strip(value)

            # values with spaces must be quoted
            if ' ' in value:
                value = '"%s"' % value

            expression += '%s:%s ' % (label, value)
            return expression

        if notebooks:
            for notebook in tools.strip(notebooks.split(',')):
                request += _formatExpression('notebook', notebook)

        if tags:
            for tag in tools.strip(tags.split(',')):
                request += _formatExpression('tag', tag)

        if date:
            date = tools.strip(date.split('-'))
            try:
                dateStruct = time.strptime(date[0] + " 00:00:00", config.DEF_DATE_FORMAT)
                request += 'created:%s ' % time.strftime("%Y%m%d", time.localtime(time.mktime(dateStruct)))
                if len(date) == 2:
                    dateStruct = time.strptime(date[1] + " 00:00:00", config.DEF_DATE_AND_TIME_FORMAT)
                request += '-created:%s ' % time.strftime("%Y%m%d", time.localtime(time.mktime(dateStruct) + 60 * 60 * 24))
            except ValueError:
                out.failureMessage('Incorrect date format in --date attribute. '
                                   'Format: %s' % time.strftime(config.DEF_DATE_FORMAT, time.strptime('19991231', "%Y%m%d")))
                return tools.exit()

        if search:
            search = tools.strip(search)
            if exact_entry or self.findExactOnUpdate:
                search = '"%s"' % search

            if content_search:
                request += "%s" % search
            else:
                request += "intitle:%s" % search

        if reminders_only:
            request += ' reminderOrder:* '
        if ignore_completed:
            request += ' -reminderDoneTime:* '

        logging.debug("Search request: %s", request)
        return request


def modifyArgsByStdinStream():
    """Parse the stdin stream for arguments"""
    content = sys.stdin.read()
    content = tools.stdinEncode(content)

    if not content:
        out.failureMessage("Input stream is empty.")
        return tools.exit()

    title = ' '.join(content.split(' ', 5)[:-1])
    title = re.sub(r'(\r\n|\r|\n)', r' ', title)
    if not title:
        out.failureMessage("Error while creating title of note from stream.")
        return tools.exit()
    elif len(title) > 50:
        title = title[0:50] + '...'

    ARGS = {
        'title': title,
        'content': content
    }

    return ('create', ARGS)


def main(args=None):
    try:
        # if terminal
        if config.IS_IN_TERMINAL:
            sys_argv = sys.argv[1:]
            if isinstance(args, list):
                sys_argv = args

            sys_argv = tools.decodeArgs(sys_argv)

            COMMAND = sys_argv[0] if len(sys_argv) >= 1 else None

            aparser = argparser(sys_argv)
            ARGS = aparser.parse()

        # if input stream
        else:
            COMMAND, ARGS = modifyArgsByStdinStream()

        # error or help
        if COMMAND is None or ARGS is False:
            return tools.exit()

        logging.debug("CLI options: %s", str(ARGS))

        # Users
        if COMMAND == 'user':
            User().user(**ARGS)

        if COMMAND == 'login':
            User().login(**ARGS)

        if COMMAND == 'logout':
            User().logout(**ARGS)

        if COMMAND == 'settings':
            User().settings(**ARGS)

        # Notes
        if COMMAND == 'create':
            Notes().create(**ARGS)

        if COMMAND == 'edit':
            Notes().edit(**ARGS)

        if COMMAND == 'remove':
            Notes().remove(**ARGS)

        if COMMAND == 'show':
            Notes().show(**ARGS)

        if COMMAND == 'find':
            Notes().find(**ARGS)

        # Notebooks
        if COMMAND == 'notebook-list':
            Notebooks().list(**ARGS)

        if COMMAND == 'notebook-create':
            Notebooks().create(**ARGS)

        if COMMAND == 'notebook-edit':
            Notebooks().edit(**ARGS)

        if COMMAND == 'notebook-remove':
            Notebooks().remove(**ARGS)

        # Tags
        if COMMAND == 'tag-list':
            Tags().list(**ARGS)

        if COMMAND == 'tag-create':
            Tags().create(**ARGS)

        if COMMAND == 'tag-edit':
            Tags().edit(**ARGS)

        if COMMAND == 'tag-remove':
            Tags().remove(**ARGS)

    except (KeyboardInterrupt, SystemExit, tools.ExitException):
        pass

    except Exception, e:
        traceback.print_exc()
        logging.error("App error: %s", str(e))

    # exit preloader
    tools.exit()

if __name__ == "__main__":
    main()
