# -*- coding: utf-8 -*-
#
# gPodder - A media aggregator and podcast client
# Copyright (c) 2005-2018 The gPodder Team
#
# gPodder is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# gPodder is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#


#
#  gpodder.gtkui.model - GUI model classes for gPodder (2009-08-13)
#  Based on code from libpodcasts.py (thp, 2005-10-29)
#

import html
import logging
import os
import re
import time

from gi.repository import GdkPixbuf, GObject, Gtk

import gpodder
from gpodder import coverart, model, query, util
from gpodder.gtkui import draw

_ = gpodder.gettext

logger = logging.getLogger(__name__)


try:
    from gi.repository import Gio
    have_gio = True
except ImportError:
    have_gio = False

# ----------------------------------------------------------


class GEpisode(model.PodcastEpisode):
    __slots__ = ()

    @property
    def title_markup(self):
        return '%s\n<small>%s</small>' % (html.escape(self.title),
                          html.escape(self.channel.title))

    @property
    def markup_new_episodes(self):
        if self.file_size > 0:
            length_str = '%s; ' % util.format_filesize(self.file_size)
        else:
            length_str = ''
        return ('<b>%s</b>\n<small>%s' + _('released %s') +
                '; ' + _('from %s') + '</small>') % (
                html.escape(re.sub('\s+', ' ', self.title)),
                html.escape(length_str),
                html.escape(self.pubdate_prop),
                html.escape(re.sub('\s+', ' ', self.channel.title)))

    @property
    def markup_delete_episodes(self):
        if self.total_time and self.current_position:
            played_string = self.get_play_info_string()
        elif not self.is_new:
            played_string = _('played')
        else:
            played_string = _('unplayed')
        downloaded_string = self.get_age_string()
        if not downloaded_string:
            downloaded_string = _('today')
        return ('<b>%s</b>\n<small>%s; %s; ' + _('downloaded %s') +
                '; ' + _('from %s') + '</small>') % (
                html.escape(self.title),
                html.escape(util.format_filesize(self.file_size)),
                html.escape(played_string),
                html.escape(downloaded_string),
                html.escape(self.channel.title))


class GPodcast(model.PodcastChannel):
    __slots__ = ()

    EpisodeClass = GEpisode


class Model(model.Model):
    PodcastClass = GPodcast

# ----------------------------------------------------------


# Singleton indicator if a row is a section
class SeparatorMarker(object): pass


class SectionMarker(object): pass


class BackgroundUpdate(object):
    def __init__(self, model, episodes, include_description):
        self.model = model
        self.episodes = episodes
        self.include_description = include_description
        self.index = 0

    def update(self):
        model = self.model
        include_description = self.include_description

        started = time.time()
        while self.episodes:
            episode = self.episodes.pop(0)
            base_fields = (
                (model.C_URL, episode.url),
                (model.C_TITLE, episode.title),
                (model.C_EPISODE, episode),
                (model.C_PUBLISHED_TEXT, episode.cute_pubdate()),
                (model.C_PUBLISHED, episode.published),
            )
            update_fields = model.get_update_fields(episode, include_description)
            model.set(model.get_iter((self.index,)), *(x for fields in (base_fields, update_fields)
                                                       for pair in fields for x in pair))
            self.index += 1

            # Check for the time limit of 20 ms after each 50 rows processed
            if self.index % 50 == 0 and (time.time() - started) > 0.02:
                break

        return bool(self.episodes)


class EpisodeListModel(Gtk.ListStore):
    C_URL, C_TITLE, C_FILESIZE_TEXT, C_EPISODE, C_STATUS_ICON, \
        C_PUBLISHED_TEXT, C_DESCRIPTION, C_TOOLTIP, \
        C_VIEW_SHOW_UNDELETED, C_VIEW_SHOW_DOWNLOADED, \
        C_VIEW_SHOW_UNPLAYED, C_FILESIZE, C_PUBLISHED, \
        C_TIME, C_TIME_VISIBLE, C_TOTAL_TIME, \
        C_LOCKED = list(range(17))

    VIEW_ALL, VIEW_UNDELETED, VIEW_DOWNLOADED, VIEW_UNPLAYED = list(range(4))

    VIEWS = ['VIEW_ALL', 'VIEW_UNDELETED', 'VIEW_DOWNLOADED', 'VIEW_UNPLAYED']

    # In which steps the UI is updated for "loading" animations
    _UI_UPDATE_STEP = .03

    # Steps for the "downloading" icon progress
    PROGRESS_STEPS = 20

    def __init__(self, config, on_filter_changed=lambda has_episodes: None):
        Gtk.ListStore.__init__(self, str, str, str, object, str, str, str,
                               str, bool, bool, bool, GObject.TYPE_INT64,
                               GObject.TYPE_INT64, str, bool,
                               GObject.TYPE_INT64, bool)

        self._config = config

        # Callback for when the filter / list changes, gets one parameter
        # (has_episodes) that is True if the list has any episodes
        self._on_filter_changed = on_filter_changed

        # Filter to allow hiding some episodes
        self._filter = self.filter_new()
        self._sorter = Gtk.TreeModelSort(self._filter)
        self._view_mode = self.VIEW_ALL
        self._search_term = None
        self._search_term_eql = None
        self._filter.set_visible_func(self._filter_visible_func)

        # Are we currently showing the "all episodes" view?
        self._all_episodes_view = False

        self.ICON_AUDIO_FILE = 'audio-x-generic'
        self.ICON_VIDEO_FILE = 'video-x-generic'
        self.ICON_IMAGE_FILE = 'image-x-generic'
        self.ICON_GENERIC_FILE = 'text-x-generic'
        self.ICON_DOWNLOADING = Gtk.STOCK_GO_DOWN
        self.ICON_DELETED = Gtk.STOCK_DELETE

        self.background_update = None
        self.background_update_tag = None

        if 'KDE_FULL_SESSION' in os.environ:
            # Workaround until KDE adds all the freedesktop icons
            # See https://bugs.kde.org/show_bug.cgi?id=233505 and
            #     http://gpodder.org/bug/553
            self.ICON_DELETED = 'archive-remove'

    def _format_filesize(self, episode):
        if episode.file_size > 0:
            return util.format_filesize(episode.file_size, digits=1)
        else:
            return None

    def _filter_visible_func(self, model, iter, misc):
        # If searching is active, set visibility based on search text
        if self._search_term is not None:
            episode = model.get_value(iter, self.C_EPISODE)
            if episode is None:
                return False

            try:
                return self._search_term_eql.match(episode)
            except Exception as e:
                return True

        if self._view_mode == self.VIEW_ALL:
            return True
        elif self._view_mode == self.VIEW_UNDELETED:
            return model.get_value(iter, self.C_VIEW_SHOW_UNDELETED)
        elif self._view_mode == self.VIEW_DOWNLOADED:
            return model.get_value(iter, self.C_VIEW_SHOW_DOWNLOADED)
        elif self._view_mode == self.VIEW_UNPLAYED:
            return model.get_value(iter, self.C_VIEW_SHOW_UNPLAYED)

        return True

    def get_filtered_model(self):
        """Returns a filtered version of this episode model

        The filtered version should be displayed in the UI,
        as this model can have some filters set that should
        be reflected in the UI.
        """
        return self._sorter

    def has_episodes(self):
        """Returns True if episodes are visible (filtered)

        If episodes are visible with the current filter
        applied, return True (otherwise return False).
        """
        return bool(len(self._filter))

    def set_view_mode(self, new_mode):
        """Sets a new view mode for this model

        After setting the view mode, the filtered model
        might be updated to reflect the new mode."""
        if self._view_mode != new_mode:
            self._view_mode = new_mode
            self._filter.refilter()
            self._on_filter_changed(self.has_episodes())

    def get_view_mode(self):
        """Returns the currently-set view mode"""
        return self._view_mode

    def set_search_term(self, new_term):
        if self._search_term != new_term:
            self._search_term = new_term
            self._search_term_eql = query.UserEQL(new_term)
            self._filter.refilter()
            self._on_filter_changed(self.has_episodes())

    def get_search_term(self):
        return self._search_term

    def _format_description(self, episode, include_description=False):
        title = episode.trimmed_title

        if episode.state != gpodder.STATE_DELETED and episode.is_new:
            yield '<b>'
            yield html.escape(title)
            yield '</b>'
        else:
            yield html.escape(title)

        if include_description:
            yield '\n'
            if self._all_episodes_view:
                yield _('from %s') % html.escape(episode.channel.title)
            else:
                description = episode.one_line_description()
                if description.startswith(title):
                    description = description[len(title):].strip()
                yield html.escape(description)

    def replace_from_channel(self, channel, include_description=False):
        """
        Add episode from the given channel to this model.
        Downloading should be a callback.
        include_description should be a boolean value (True if description
        is to be added to the episode row, or False if not)
        """

        # Remove old episodes in the list store
        self.clear()

        self._all_episodes_view = getattr(channel, 'ALL_EPISODES_PROXY', False)

        # Avoid gPodder bug 1291
        if channel is None:
            episodes = []
        else:
            episodes = channel.get_all_episodes()

        # Always make a copy, so we can pass the episode list to BackgroundUpdate
        episodes = list(episodes)

        for _ in range(len(episodes)):
            self.append()

        self._update_from_episodes(episodes, include_description)

    def _update_from_episodes(self, episodes, include_description):
        if self.background_update_tag is not None:
            GObject.source_remove(self.background_update_tag)

        self.background_update = BackgroundUpdate(self, episodes, include_description)
        self.background_update_tag = GObject.idle_add(self._update_background)

    def _update_background(self):
        if self.background_update is not None:
            if self.background_update.update():
                return True

            self.background_update = None
            self.background_update_tag = None
            self._on_filter_changed(self.has_episodes())

        return False

    def update_all(self, include_description=False):
        if self.background_update is None:
            episodes = [row[self.C_EPISODE] for row in self]
        else:
            # Update all episodes that have already been initialized...
            episodes = [row[self.C_EPISODE] for index, row in enumerate(self) if index < self.background_update.index]
            # ...and also include episodes that still need to be initialized
            episodes.extend(self.background_update.episodes)

        self._update_from_episodes(episodes, include_description)

    def update_by_urls(self, urls, include_description=False):
        for row in self:
            if row[self.C_URL] in urls:
                self.update_by_iter(row.iter, include_description)

    def update_by_filter_iter(self, iter, include_description=False):
        # Convenience function for use by "outside" methods that use iters
        # from the filtered episode list model (i.e. all UI things normally)
        iter = self._sorter.convert_iter_to_child_iter(iter)
        self.update_by_iter(self._filter.convert_iter_to_child_iter(iter),
                include_description)

    def get_update_fields(self, episode, include_description):
        show_bullet = False
        show_padlock = False
        show_missing = False
        status_icon = None
        tooltip = []
        view_show_undeleted = True
        view_show_downloaded = False
        view_show_unplayed = False
        icon_theme = Gtk.IconTheme.get_default()

        if episode.downloading:
            tooltip.append('%s %d%%' % (_('Downloading'),
                int(episode.download_task.progress * 100)))

            index = int(self.PROGRESS_STEPS * episode.download_task.progress)
            status_icon = 'gpodder-progress-%d' % index

            view_show_downloaded = True
            view_show_unplayed = True
        else:
            if episode.state == gpodder.STATE_DELETED:
                tooltip.append(_('Deleted'))
                status_icon = self.ICON_DELETED
                view_show_undeleted = False
            elif episode.state == gpodder.STATE_NORMAL and \
                    episode.is_new:
                tooltip.append(_('New episode'))
                view_show_downloaded = True
                view_show_unplayed = True
            elif episode.state == gpodder.STATE_DOWNLOADED:
                tooltip = []
                view_show_downloaded = True
                view_show_unplayed = episode.is_new
                show_bullet = episode.is_new
                show_padlock = episode.archive
                show_missing = not episode.file_exists()
                filename = episode.local_filename(create=False, check_only=True)

                file_type = episode.file_type()
                if file_type == 'audio':
                    tooltip.append(_('Downloaded episode'))
                    status_icon = self.ICON_AUDIO_FILE
                elif file_type == 'video':
                    tooltip.append(_('Downloaded video episode'))
                    status_icon = self.ICON_VIDEO_FILE
                elif file_type == 'image':
                    tooltip.append(_('Downloaded image'))
                    status_icon = self.ICON_IMAGE_FILE
                else:
                    tooltip.append(_('Downloaded file'))
                    status_icon = self.ICON_GENERIC_FILE

                # Try to find a themed icon for this file
                # doesn't work on win32 (opus files are showed as text)
                if filename is not None and have_gio and not gpodder.ui.win32:
                    file = Gio.File.new_for_path(filename)
                    if file.query_exists():
                        file_info = file.query_info('*', Gio.FileQueryInfoFlags.NONE, None)
                        icon = file_info.get_icon()
                        for icon_name in icon.get_names():
                            if icon_theme.has_icon(icon_name):
                                status_icon = icon_name
                                break

                if show_missing:
                    tooltip.append(_('missing file'))
                else:
                    if show_bullet:
                        if file_type == 'image':
                            tooltip.append(_('never displayed'))
                        elif file_type in ('audio', 'video'):
                            tooltip.append(_('never played'))
                        else:
                            tooltip.append(_('never opened'))
                    else:
                        if file_type == 'image':
                            tooltip.append(_('displayed'))
                        elif file_type in ('audio', 'video'):
                            tooltip.append(_('played'))
                        else:
                            tooltip.append(_('opened'))
                    if show_padlock:
                        tooltip.append(_('deletion prevented'))

                if episode.total_time > 0 and episode.current_position:
                    tooltip.append('%d%%' % (100. * float(episode.current_position) /
                                             float(episode.total_time),))

        if episode.total_time:
            total_time = util.format_time(episode.total_time)
            if total_time:
                tooltip.append(total_time)

        tooltip = ', '.join(tooltip)

        description = ''.join(self._format_description(episode, include_description))
        return (
                (self.C_STATUS_ICON, status_icon),
                (self.C_VIEW_SHOW_UNDELETED, view_show_undeleted),
                (self.C_VIEW_SHOW_DOWNLOADED, view_show_downloaded),
                (self.C_VIEW_SHOW_UNPLAYED, view_show_unplayed),
                (self.C_DESCRIPTION, description),
                (self.C_TOOLTIP, tooltip),
                (self.C_TIME, episode.get_play_info_string()),
                (self.C_TIME_VISIBLE, bool(episode.total_time)),
                (self.C_TOTAL_TIME, episode.total_time),
                (self.C_LOCKED, episode.archive),
                (self.C_FILESIZE_TEXT, self._format_filesize(episode)),
                (self.C_FILESIZE, episode.file_size),
        )

    def update_by_iter(self, iter, include_description=False):
        episode = self.get_value(iter, self.C_EPISODE)
        if episode is not None:
            self.set(iter, *(x for pair in self.get_update_fields(episode, include_description) for x in pair))


class PodcastChannelProxy(object):
    ALL_EPISODES_PROXY = True

    def __init__(self, db, config, channels):
        self._db = db
        self._config = config
        self.channels = channels
        self.title = _('All episodes')
        self.description = _('from all podcasts')
        # self.parse_error = ''
        self.url = ''
        self.section = ''
        self.id = None
        self.cover_file = coverart.CoverDownloader.ALL_EPISODES_ID
        self.cover_url = None
        self.auth_username = None
        self.auth_password = None
        self.pause_subscription = False
        self.sync_to_mp3_player = False
        self.cover_thumb = None
        self.auto_archive_episodes = False

    def get_statistics(self):
        # Get the total statistics for all channels from the database
        return self._db.get_podcast_statistics()

    def get_all_episodes(self):
        """Returns a generator that yields every episode"""
        return Model.sort_episodes_by_pubdate((e for c in self.channels
                for e in c.get_all_episodes()), True)

    def save(self):
        pass


class PodcastListModel(Gtk.ListStore):
    C_URL, C_TITLE, C_DESCRIPTION, C_PILL, C_CHANNEL, \
        C_COVER, C_ERROR, C_PILL_VISIBLE, \
        C_VIEW_SHOW_UNDELETED, C_VIEW_SHOW_DOWNLOADED, \
        C_VIEW_SHOW_UNPLAYED, C_HAS_EPISODES, C_SEPARATOR, \
        C_DOWNLOADS, C_COVER_VISIBLE, C_SECTION = list(range(16))

    SEARCH_COLUMNS = (C_TITLE, C_DESCRIPTION, C_SECTION)

    @classmethod
    def row_separator_func(cls, model, iter):
        return model.get_value(iter, cls.C_SEPARATOR)

    def __init__(self, cover_downloader):
        Gtk.ListStore.__init__(self, str, str, str, GdkPixbuf.Pixbuf,
                object, GdkPixbuf.Pixbuf, str, bool, bool, bool, bool,
                bool, bool, int, bool, str)

        # Filter to allow hiding some episodes
        self._filter = self.filter_new()
        self._view_mode = -1
        self._search_term = None
        self._filter.set_visible_func(self._filter_visible_func)

        self._cover_cache = {}
        self._max_image_side = 40
        self._cover_downloader = cover_downloader

        self.ICON_DISABLED = 'gtk-media-pause'

    def _filter_visible_func(self, model, iter, misc):
        # If searching is active, set visibility based on search text
        if self._search_term is not None:
            if model.get_value(iter, self.C_CHANNEL) == SectionMarker:
                return True
            key = self._search_term.lower()
            columns = (model.get_value(iter, c) for c in self.SEARCH_COLUMNS)
            return any((key in c.lower() for c in columns if c is not None))

        if model.get_value(iter, self.C_SEPARATOR):
            return True
        elif self._view_mode == EpisodeListModel.VIEW_ALL:
            return model.get_value(iter, self.C_HAS_EPISODES)
        elif self._view_mode == EpisodeListModel.VIEW_UNDELETED:
            return model.get_value(iter, self.C_VIEW_SHOW_UNDELETED)
        elif self._view_mode == EpisodeListModel.VIEW_DOWNLOADED:
            return model.get_value(iter, self.C_VIEW_SHOW_DOWNLOADED)
        elif self._view_mode == EpisodeListModel.VIEW_UNPLAYED:
            return model.get_value(iter, self.C_VIEW_SHOW_UNPLAYED)

        return True

    def get_filtered_model(self):
        """Returns a filtered version of this episode model

        The filtered version should be displayed in the UI,
        as this model can have some filters set that should
        be reflected in the UI.
        """
        return self._filter

    def set_view_mode(self, new_mode):
        """Sets a new view mode for this model

        After setting the view mode, the filtered model
        might be updated to reflect the new mode."""
        if self._view_mode != new_mode:
            self._view_mode = new_mode
            self._filter.refilter()

    def get_view_mode(self):
        """Returns the currently-set view mode"""
        return self._view_mode

    def set_search_term(self, new_term):
        if self._search_term != new_term:
            self._search_term = new_term
            self._filter.refilter()

    def get_search_term(self):
        return self._search_term

    def enable_separators(self, channeltree):
        channeltree.set_row_separator_func(self._show_row_separator)

    def _show_row_separator(self, model, iter):
        return model.get_value(iter, self.C_SEPARATOR)

    def set_max_image_size(self, size):
        self._max_image_side = size
        self._cover_cache = {}

    def _resize_pixbuf_keep_ratio(self, url, pixbuf):
        """
        Resizes a GTK Pixbuf but keeps its aspect ratio.
        Returns None if the pixbuf does not need to be
        resized or the newly resized pixbuf if it does.
        """
        changed = False
        result = None

        if url in self._cover_cache:
            return self._cover_cache[url]

        # Resize if too wide
        if pixbuf.get_width() > self._max_image_side:
            f = float(self._max_image_side) / pixbuf.get_width()
            (width, height) = (int(pixbuf.get_width() * f), int(pixbuf.get_height() * f))
            pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
            changed = True

        # Resize if too high
        if pixbuf.get_height() > self._max_image_side:
            f = float(self._max_image_side) / pixbuf.get_height()
            (width, height) = (int(pixbuf.get_width() * f), int(pixbuf.get_height() * f))
            pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
            changed = True

        if changed:
            self._cover_cache[url] = pixbuf
            result = pixbuf

        return result

    def _resize_pixbuf(self, url, pixbuf):
        if pixbuf is None:
            return None

        return self._resize_pixbuf_keep_ratio(url, pixbuf) or pixbuf

    def _overlay_pixbuf(self, pixbuf, icon):
        try:
            icon_theme = Gtk.IconTheme.get_default()
            emblem = icon_theme.load_icon(icon, self._max_image_side / 2, 0)
            (width, height) = (emblem.get_width(), emblem.get_height())
            xpos = pixbuf.get_width() - width
            ypos = pixbuf.get_height() - height
            if ypos < 0:
                # need to resize overlay for none standard icon size
                emblem = icon_theme.load_icon(icon, pixbuf.get_height() - 1, 0)
                (width, height) = (emblem.get_width(), emblem.get_height())
                xpos = pixbuf.get_width() - width
                ypos = pixbuf.get_height() - height
            emblem.composite(pixbuf, xpos, ypos, width, height, xpos, ypos, 1, 1, GdkPixbuf.InterpType.BILINEAR, 255)
        except:
            pass

        return pixbuf

    def _get_cached_thumb(self, channel):
        if channel.cover_thumb is None:
            return None

        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(channel.cover_thumb)
            loader.close()
            pixbuf = loader.get_pixbuf()
            if self._max_image_side not in (pixbuf.get_width(), pixbuf.get_height()):
                logger.debug("cached thumb wrong size: %r != %i", (pixbuf.get_width(), pixbuf.get_height()), self._max_image_side)
                return None
        except Exception as e:
            logger.warn('Could not load cached cover art for %s', channel.url, exc_info=True)
            channel.cover_thumb = None
            channel.save()
            return None

    def _save_cached_thumb(self, channel, pixbuf):
        bufs = []

        def save_callback(buf, length, user_data):
            user_data.append(buf)
            return True
        pixbuf.save_to_callbackv(save_callback, bufs, 'png', [None], [])
        channel.cover_thumb = bytes(b''.join(bufs))
        channel.save()

    def _get_cover_image(self, channel, add_overlay=False):
        if self._cover_downloader is None:
            return None

        pixbuf_overlay = self._get_cached_thumb(channel)

        if pixbuf_overlay is None:
            pixbuf = self._cover_downloader.get_cover(channel, avoid_downloading=True)
            pixbuf_overlay = self._resize_pixbuf(channel.url, pixbuf)
            self._save_cached_thumb(channel, pixbuf_overlay)

        if add_overlay and channel.pause_subscription:
            pixbuf_overlay = self._overlay_pixbuf(pixbuf_overlay, self.ICON_DISABLED)
            pixbuf_overlay.saturate_and_pixelate(pixbuf_overlay, 0.0, False)

        return pixbuf_overlay

    def _get_pill_image(self, channel, count_downloaded, count_unplayed):
        if count_unplayed > 0 or count_downloaded > 0:
            return draw.draw_pill_pixbuf(str(count_unplayed), str(count_downloaded), widget=self.widget)
        else:
            return None

    def _format_description(self, channel, total, deleted,
            new, downloaded, unplayed):
        title_markup = html.escape(channel.title)
        if not channel.pause_subscription:
            description_markup = html.escape(util.get_first_line(channel.description) or ' ')
        else:
            description_markup = html.escape(_('Subscription paused'))
        d = []
        if new:
            d.append('<span weight="bold">')
        d.append(title_markup)
        if new:
            d.append('</span>')

        if description_markup.strip():
            return ''.join(d + ['\n', '<small>', description_markup, '</small>'])
        else:
            return ''.join(d)

    def _format_error(self, channel):
        # if channel.parse_error:
        #     return str(channel.parse_error)
        # else:
        #     return None
        return None

    def set_channels(self, db, config, channels):
        # Clear the model and update the list of podcasts
        self.clear()

        def channel_to_row(channel, add_overlay=False):
            return (channel.url, '', '', None, channel,
                    self._get_cover_image(channel, add_overlay), '', True,
                    True, True, True, True, False, 0, True, '')

        if config.podcast_list_view_all and channels:
            all_episodes = PodcastChannelProxy(db, config, channels)
            iter = self.append(channel_to_row(all_episodes))
            self.update_by_iter(iter)

            # Separator item
            if not config.podcast_list_sections:
                self.append(('', '', '', None, SeparatorMarker, None, '',
                    True, True, True, True, True, True, 0, False, ''))

        def key_func(pair):
            section, podcast = pair
            return (section, model.Model.podcast_sort_key(podcast))

        if config.podcast_list_sections:
            def convert(channels):
                for channel in channels:
                    yield (channel.group_by, channel)
        else:
            def convert(channels):
                for channel in channels:
                    yield (None, channel)

        added_sections = []
        old_section = None
        for section, channel in sorted(convert(channels), key=key_func):
            if old_section != section:
                it = self.append(('-', section, '', None, SectionMarker, None,
                    '', True, True, True, True, True, False, 0, False, section))
                added_sections.append(it)
                old_section = section

            iter = self.append(channel_to_row(channel, True))
            self.update_by_iter(iter)

        # Update section header stats only after all podcasts
        # have been added to the list to get the stats right
        for it in added_sections:
            self.update_by_iter(it)

    def get_filter_path_from_url(self, url):
        # Return the path of the filtered model for a given URL
        child_path = self.get_path_from_url(url)
        if child_path is None:
            return None
        else:
            return self._filter.convert_child_path_to_path(child_path)

    def get_path_from_url(self, url):
        # Return the tree model path for a given URL
        if url is None:
            return None

        for row in self:
            if row[self.C_URL] == url:
                return row.path
        return None

    def update_first_row(self):
        # Update the first row in the model (for "all episodes" updates)
        self.update_by_iter(self.get_iter_first())

    def update_by_urls(self, urls):
        # Given a list of URLs, update each matching row
        for row in self:
            if row[self.C_URL] in urls:
                self.update_by_iter(row.iter)

    def iter_is_first_row(self, iter):
        iter = self._filter.convert_iter_to_child_iter(iter)
        path = self.get_path(iter)
        return (path == Gtk.TreePath.new_first())

    def update_by_filter_iter(self, iter):
        self.update_by_iter(self._filter.convert_iter_to_child_iter(iter))

    def update_all(self):
        for row in self:
            self.update_by_iter(row.iter)

    def update_sections(self):
        for row in self:
            if row[self.C_CHANNEL] is SectionMarker:
                self.update_by_iter(row.iter)

    def update_by_iter(self, iter):
        if iter is None:
            return

        # Given a GtkTreeIter, update volatile information
        channel = self.get_value(iter, self.C_CHANNEL)

        if channel is SectionMarker:
            section = self.get_value(iter, self.C_TITLE)

            # This row is a section header - update its visibility flags
            channels = [c for c in (row[self.C_CHANNEL] for row in self)
                    if isinstance(c, GPodcast) and c.section == section]

            # Calculate the stats over all podcasts of this section
            if len(channels) is 0:
                total = deleted = new = downloaded = unplayed = 0
            else:
                total, deleted, new, downloaded, unplayed = list(map(sum,
                        list(zip(*[c.get_statistics() for c in channels]))))

            # We could customized the section header here with the list
            # of channels and their stats (i.e. add some "new" indicator)
            description = '<span size="16000"> </span><b>%s</b>' % (
                    html.escape(section))

            self.set(
                iter,
                self.C_DESCRIPTION, description,
                self.C_SECTION, section,
                self.C_VIEW_SHOW_UNDELETED, total - deleted > 0,
                self.C_VIEW_SHOW_DOWNLOADED, downloaded + new > 0,
                self.C_VIEW_SHOW_UNPLAYED, unplayed + new > 0)

        if (not isinstance(channel, GPodcast) and
                not isinstance(channel, PodcastChannelProxy)):
            return

        total, deleted, new, downloaded, unplayed = channel.get_statistics()
        description = self._format_description(channel, total, deleted, new,
                downloaded, unplayed)

        pill_image = self._get_pill_image(channel, downloaded, unplayed)

        self.set(iter,
                self.C_TITLE, channel.title,
                self.C_DESCRIPTION, description,
                self.C_SECTION, channel.section,
                self.C_ERROR, self._format_error(channel),
                self.C_PILL, pill_image,
                self.C_PILL_VISIBLE, pill_image is not None,
                self.C_VIEW_SHOW_UNDELETED, total - deleted > 0,
                self.C_VIEW_SHOW_DOWNLOADED, downloaded + new > 0,
                self.C_VIEW_SHOW_UNPLAYED, unplayed + new > 0,
                self.C_HAS_EPISODES, total > 0,
                self.C_DOWNLOADS, downloaded)

    def clear_cover_cache(self, podcast_url):
        if podcast_url in self._cover_cache:
            logger.info('Clearing cover from cache: %s', podcast_url)
            del self._cover_cache[podcast_url]

    def add_cover_by_channel(self, channel, pixbuf):
        if pixbuf is None:
            return
        # Remove older images from cache
        self.clear_cover_cache(channel.url)

        # Resize and add the new cover image
        pixbuf = self._resize_pixbuf(channel.url, pixbuf)
        self._save_cached_thumb(channel, pixbuf)

        if channel.pause_subscription:
            pixbuf = self._overlay_pixbuf(pixbuf, self.ICON_DISABLED)
            pixbuf.saturate_and_pixelate(pixbuf, 0.0, False)

        for row in self:
            if row[self.C_URL] == channel.url:
                row[self.C_COVER] = pixbuf
                break
