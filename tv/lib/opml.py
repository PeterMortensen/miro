# Miro - an RSS based video player application
# Copyright (C) 2005-2010 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

import os
import logging

from xml.dom import minidom
from xml.sax import saxutils
from xml.parsers import expat
from datetime import datetime
from StringIO import StringIO

from miro import subscription
from miro import feed
from miro import guide
from miro import prefs
from miro import config
from miro import folder
from miro import dialogs
from miro import eventloop
from miro import tabs

from miro.gtcache import gettext as _
from miro.gtcache import ngettext
from miro.xhtmltools import urlencode
from miro.plat.utils import filenameToUnicode

class Exporter(object):
    def __init__(self):
        self.io = StringIO()
        self.currentFolder = None

    @eventloop.as_idle
    def export_subscriptions(self, pathname):
        now = datetime.now()

        self.io.write(u'<?xml version="1.0" encoding="utf-8" ?>\n')
        self.io.write(u'<!-- OPML generated by Miro v%s on %s -->\n' % (config.get(prefs.APP_VERSION), now.ctime()))
        self.io.write(u'<opml version="2.0"\n')
        self.io.write(u'      xmlns:miro="http://getmiro.com/opml/subscriptions">\n')
        self.io.write(u'<head>\n')
        self.io.write(u'\t<title>%s</title>\n' %
                filenameToUnicode(os.path.basename(pathname)))
        self.io.write(u'\t<dateCreated>%s</dateCreated>\n' % now.ctime())
        self.io.write(u'\t<docs>http://www.opml.org/spec2</docs>\n')
        self.io.write(u'</head>\n')
        self.io.write(u'<body>\n')

        tabOrder = tabs.TabOrder.video_feed_order()
        audioTabOrder = tabs.TabOrder.audio_feed_order()
        for obj in tabOrder.get_all_tabs() + audioTabOrder.get_all_tabs():
            if isinstance(obj, folder.ChannelFolder):
                self._open_folder_entry(obj)
            elif isinstance(obj, feed.Feed):
                self._write_feed_entry(obj)

        if self.currentFolder is not None:
            self._close_folder_entry()

        site_tab_order = tabs.TabOrder.site_order()
        for obj in site_tab_order.get_all_tabs():
            self._write_site_entry(obj)

        self.io.write(u'</body>\n')
        self.io.write(u'</opml>\n')

        try:
            f = open(pathname, "w")
            f.write(self.io.getvalue().encode('utf-8'))
            f.close()
        except (SystemExit, KeyboardInterrupt):
            raise
        except:
            logging.exception("Could not create miro_subscriptions.opml file.")

    def _open_folder_entry(self, folder):
        if self.currentFolder is not None:
            self._close_folder_entry()
        self.currentFolder = folder
        self.io.write(u'\t<outline text=%s miro:section=%s>\n' % (
                saxutils.quoteattr(folder.get_title()),
                saxutils.quoteattr(folder.section)))

    def _close_folder_entry(self):
        self.io.write(u'\t</outline>\n')

    def _write_feed_entry(self, thefeed):
        if self.currentFolder is not None and thefeed.get_folder() is None:
            self._close_folder_entry()
            self.currentFolder = None
        if self.currentFolder is None:
            spacer = u'\t'
        else:
            spacer = u'\t\t'

        # skip watched folders and other non-RSSFeedImpl derivatives
        if not isinstance(thefeed.get_actual_feed(), feed.RSSFeedImpl):
            return

        extraArgs = []

        search_term = thefeed.get_search_term()
        if search_term:
            extraArgs.append('miro:searchTerm=%s' % saxutils.quoteattr(search_term))

        autoDownload = thefeed.get_autodownload_mode()
        if autoDownload != 'new':
            extraArgs.append('miro:autoDownload=%s' % saxutils.quoteattr(autoDownload))

        expiryTime = thefeed.expire
        if expiryTime != 'system':
            if expiryTime == 'feed':
                expiryTime = unicode(thefeed.get_expiration_time())
            extraArgs.append('miro:expiryTime=%s' % saxutils.quoteattr(expiryTime))
        extraArgs = u' '.join(extraArgs)

        self.io.write(u'%s<outline type="rss" text=%s xmlUrl=%s miro:section=%s %s/>\n' % (
                spacer,
                saxutils.quoteattr(thefeed.get_title()),
                saxutils.quoteattr(thefeed.get_base_url()),
                saxutils.quoteattr(thefeed.section),
                extraArgs))

    def _write_site_entry(self, site):
        quoted_url = saxutils.quoteattr(site.url)
        if site.title:
            quoted_text = saxutils.quoteattr(site.title)
        else:
            quoted_text = quoted_url

        self.io.write(
            u'\t<outline type="link" text=%s url=%s />\n' % (
                    quoted_text,
                    quoted_url))

# =============================================================================

class Importer(object):
    def __init__(self):
        self.currentFolder = None
        self.ignoredFeeds = 0
        self.importedFeeds = 0
        self.addedChannels = 0

    @eventloop.as_idle
    def import_subscriptions(self, pathname, show_summary=True):
        f = open(pathname, "r")
        content = f.read()
        f.close()

        try:
            subscriptions = self.import_content(content)
            self.result = subscription.Subscriber().add_subscriptions(subscriptions)
            if show_summary:
                self.show_import_summary()
        except expat.ExpatError:
            self.show_xml_error()

    def import_content(self, content):
        dom = minidom.parseString(content)
        root = dom.documentElement
        body = root.getElementsByTagName("body")
        if not body:
            dom.unlink()
            return []
        body = body.pop()
        subscriptions = self._walk_outline(body)
        dom.unlink()
        return subscriptions

    def show_xml_error(self):
        title = _("OPML Import failed")
        message = _("The selected OPML file appears to be invalid.  Import was interrupted.")
        dialog = dialogs.MessageBoxDialog(title, message)
        dialog.run()

    def show_import_summary(self):
        importedFeeds = len(self.result[0].get('feed', []))
        ignoredFeeds = len(self.result[1].get('feed', []))
        title = _("OPML Import summary")
        message = ngettext("Successfully imported %(count)d feed.",
                           "Successfully imported %(count)d feeds.",
                           importedFeeds,
                           {"count": importedFeeds})
        if self.ignoredFeeds > 0:
            message += "\n"
            message += ngettext("Skipped %(count)d feed already present.",
                                "Skipped %(count)d feeds already present.",
                                ignoredFeeds,
                                {"count": ignoredFeeds})
        dialog = dialogs.MessageBoxDialog(title, message)
        dialog.run()

    def _walk_outline(self, node, subscriptions=None):
        if subscriptions is None:
            subscriptions = []
        try:
            children = node.childNodes
            for child in children:
                if hasattr(child, 'getAttribute'):
                    if child.getAttribute('type') == 'link':
                        subscriptions.append(self._handle_site_entry(child))
                    elif child.getAttribute('type') == 'guide':
                        subscriptions.append(self._handle_guide_entry(child))
                    elif child.getAttribute('type') == 'download':
                        subscriptions.append(self._handle_download_entry(child))
                    elif child.hasAttribute("xmlUrl"):
                        subscriptions.append(self._handle_feed_entry(child))
                    else:
                        subscriptions.append(self._handle_folder_entry(child))
            return subscriptions
        except Exception, e:
            print e
            pass

    def _handle_site_entry(self, entry):
        url = entry.getAttribute("url")
        title = entry.getAttribute("text")
        return {'type': 'site', 'url': url, 'title': title}

    def _handle_guide_entry(self, entry):
        url = entry.getAttribute("xmlUrl")
        title = entry.getAttribute('text')
        return {'type': 'site', 'url': url, 'title': title}

    def _handle_download_entry(self, entry):
        url = entry.getAttribute('xmlUrl')
        subscription = {'type': 'download', 'url': url}
        title = entry.getAttribute('title')
        if title and title != url:
            subscription['title'] = title

        keys = ('title', 'description', 'length', 'thumbnail', 'feed', 'link',
                'type')
        additionalData = {}
        for key in keys:
            data = entry.getAttribute('additional%s' % key.title())
            if data:
                additionalData[key] = data

        if 'type' in additionalData: # type key needs to be converted to
                                     # mime_type
            mime_type = additionalData.pop('type')
            subscription['mime_type'] = mime_type
        subscription.update(additionalData) # other keys can pass through
        return subscription

    def _handle_feed_entry(self, entry):
        url = entry.getAttribute("xmlUrl")
        subscription = {'type': 'feed', 'url': url}

        search_term = entry.getAttribute('miro:searchTerm')
        if search_term:
            subscription['search_term'] = search_term

        section = unicode(entry.getAttribute('miro:section'))
        if section not in (u'audio', u'video'):
            section = u''
        if section:
            subscription['section'] = section

        title = entry.getAttribute("text")
        if title is not None and title != '' and title != url:
            subscription['title'] = title

        auto_download_mode = (
            entry.getAttribute("miro:autoDownload")
            or entry.getAttribute("autoDownload"))
        if auto_download_mode is not None and auto_download_mode in ['all',
                'new', 'off']:
            subscription['auto_download_mode'] = auto_download_mode
        expiry_time = (
            entry.getAttribute("miro:expiryTime")
            or entry.getAttribute("expiryTime"))
        if expiry_time is not None and expiry_time != '':
            if expiry_time in ('system', 'never'):
                subscription['expiry_time'] = expiry_time
            else:
                try:
                    expiry_time = int(expiry_time)
                except ValueError:
                    pass
                else:
                    subscription['expiry_time'] = expiry_time
        return subscription

    def _handle_folder_entry(self, entry):
        title = entry.getAttribute("text")
        section = unicode(entry.getAttribute("miro:section"))
        if section not in (u'audio', u'video'):
            section = u'video'
        folder = {'type': 'folder', 'title': title, 'section': section}
        children = self._walk_outline(entry)
        if children is not None:
            folder['children'] = children
        else:
            folder['children'] = []
        return folder

