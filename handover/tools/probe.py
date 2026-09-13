#!/usr/bin/env python3
"""Drive one PMS through python-plexapi READ calls and record what plexapi saw.

Output: JSON of probes -> {requests, error, objects}. Each object records the
class plexapi built, the XML tag, its attribute keys, the child element tags
with their attribute keys (two levels), and which plexapi attributes are None.
"""
import os
import json
import re
import sys
import time
import traceback

import requests
from plexapi.server import PlexServer
from plexapi.base import PlexObject

SERVERS = {
    'sim': (os.environ.get('PLEXSIM_BASE', 'http://127.0.0.1:32542'), 'plexsim-test-token', False,
            dict(movie=1007, movie2=1008, movie3=1009, show=20001, season=30001, episode=1001, episode2=1002)),
    'ref': ('http://kids:32400', os.environ["PLEXLAB_TOKEN"], True,
            dict(movie=4, movie2=18, movie3=1, show=6, season=7, episode=8, episode2=9)),
    'fun': ('http://fun:32400', os.environ["FUN_TOKEN"], True,
            dict(movie=1116499, movie2=1150643, movie3=1060797, show=981927, season=None, episode=None, episode2=1142749)),
}

# GET paths that WRITE on a real PMS, or leave the server, or are otherwise off limits.
WRITE_GET = re.compile(
    r'/:/(scrobble|unscrobble|rate|progress|timeline|plugins)|/refresh|/analyze|/emptyTrash|/optimize|'
    r'/clean|/match(\?|$)|/unmatch|/split|/merge|/actions/|/updater|/myplex/claim|/downloadQueue|'
    r'/playQueues|/security/token|/sync|/diagnostics|'
    r'/\:/prefs\?|/library/metadata/\d+/(poster|art|theme|logo)\?url|/library/metadata/\d+/(posters|arts)\?url|'
    r'/servers/|/resources\?includeHttps|/transcode/universal/(start|ping|stop)|/video/:/transcode')


class GuardSession(requests.Session):
    def __init__(self, name, readonly, base_host=''):
        super().__init__()
        self.base_host = base_host
        self.name = name
        self.readonly = readonly
        self.log = []

    def request(self, method, url, **kw):
        if self.readonly:
            if self.base_host not in url:
                raise RuntimeError(f'REFUSED off-host request on {self.name}: {method} {url}')
            if method.upper() != 'GET':
                raise RuntimeError(f'REFUSED non-GET on {self.name}: {method} {url}')
            path = url.split('://', 1)[1].split('/', 1)[1] if '://' in url else url
            if WRITE_GET.search('/' + path):
                raise RuntimeError(f'REFUSED write-shaped GET on {self.name}: {url}')
        t0 = time.time()
        r = super().request(method, url, **kw)
        body = r.text if 'xml' in (r.headers.get('Content-Type') or '') or 'json' in (r.headers.get('Content-Type') or '') else f'<{len(r.content)} bytes {r.headers.get("Content-Type")}>'
        self.log.append(dict(method=method, url=url, status=r.status_code, ms=int((time.time() - t0) * 1000),
                             ctype=r.headers.get('Content-Type'), body=body[:400000]))
        return r


def describe_elem(el, depth=2):
    d = dict(tag=el.tag, attrs=sorted(el.attrib.keys()))
    if depth > 0:
        kids = {}
        for c in el:
            kids.setdefault(c.tag, []).append(describe_elem(c, depth - 1))
        # collapse: for each tag, union of attribute keys, count, and one sample of nested children
        coll = {}
        for tag, lst in kids.items():
            union = sorted(set().union(*[set(x['attrs']) for x in lst]))
            sub = {}
            for x in lst:
                for st, sl in (x.get('children') or {}).items():
                    prev = sub.get(st)
                    if prev is None:
                        sub[st] = sl
                    else:
                        prev['attrs'] = sorted(set(prev['attrs']) | set(sl['attrs']))
                        prev['count'] += sl['count']
                        for k, v in (sl.get('children') or {}).items():
                            if k in prev.get('children', {}):
                                prev['children'][k]['attrs'] = sorted(set(prev['children'][k]['attrs']) | set(v['attrs']))
                            else:
                                prev.setdefault('children', {})[k] = v
            coll[tag] = dict(attrs=union, count=len(lst), children=sub)
        d['children'] = coll
    return d


def describe(obj):
    if isinstance(obj, PlexObject):
        el = obj._data
        d = describe_elem(el)
        d['cls'] = type(obj).__name__
        d['initpath'] = getattr(obj, '_initpath', None)
        nones = sorted(k for k, v in obj.__dict__.items() if not k.startswith('_') and v is None)
        d['none_attrs'] = nones
        d['sample'] = {k: el.attrib.get(k) for k in ('ratingKey', 'title', 'type', 'key', 'id', 'name', 'hubIdentifier')}
        return d
    return dict(cls=type(obj).__name__, repr=repr(obj)[:300])


def main():
    name = sys.argv[1]
    base, token, readonly, keys = SERVERS[name]
    sess = GuardSession(name, readonly, base.split('://',1)[1])
    results = []

    def run(label, fn):
        start = len(sess.log)
        rec = dict(probe=label, error=None, objects=[])
        try:
            out = fn()
            if isinstance(out, (list, tuple)):
                rec['objects'] = [describe(o) for o in out]
                rec['count'] = len(out)
            elif out is None:
                rec['objects'] = []
                rec['count'] = 0
            elif isinstance(out, PlexObject):
                rec['objects'] = [describe(out)]
                rec['count'] = 1
            else:
                rec['value'] = repr(out)[:500]
        except Exception as e:
            rec['error'] = f'{type(e).__name__}: {str(e)[:600]}'
            rec['trace'] = traceback.format_exc()[-1500:]
        rec['requests'] = sess.log[start:]
        results.append(rec)
        print(f'{label:55s} {"ERR " + rec["error"][:80] if rec["error"] else ("n=" + str(rec.get("count", "-")))}', file=sys.stderr)
        return None if rec['error'] else out

    server = None
    def connect():
        nonlocal server
        server = PlexServer(base, token, session=sess, timeout=60)
        return server
    run('PlexServer()', connect)
    if server is None:
        json.dump(dict(server=name, results=results), open(f'out_{name}.json', 'w'), indent=1)
        return

    run('server.identity()', lambda: server.identity())
    run('server.account()', lambda: server.account())
    run('server.library', lambda: server.library)
    sections = run('library.sections()', lambda: server.library.sections()) or []
    if name == 'fun':
        sections = [s for s in sections if s.key in ('15', '19')]
    run('library.hubs()', lambda: server.library.hubs())
    run('library.onDeck()', lambda: server.library.onDeck())
    run('library.recentlyAdded()', lambda: server.library.recentlyAdded())
    run('library.search(title="e")', lambda: server.library.search(title='e', maxresults=5))
    run('library.search(libtype=episode)', lambda: server.library.search(libtype='episode', maxresults=5))
    run('library.tags(genre)', lambda: server.library.tags('genre'))
    run('library.history()', lambda: server.library.history(maxresults=5))
    run('server.search("the")', lambda: server.search('the'))
    run('server.search("s", mediatype=movie)', lambda: server.search('s', mediatype='movie'))
    run('server.search("s", mediatype=show)', lambda: server.search('s', mediatype='show'))
    run('server.search("s", mediatype=episode)', lambda: server.search('s', mediatype='episode'))
    run('server.search("s", mediatype=collection)', lambda: server.search('s', mediatype='collection'))
    run('server.search("s", mediatype=playlist)', lambda: server.search('s', mediatype='playlist'))
    run('server.search("s", limit=2)', lambda: server.search('s', limit=2))
    run('server.playlists()', lambda: server.playlists())
    run('server.playlists(video)', lambda: server.playlists(playlistType='video'))
    run('server.history()', lambda: server.history(maxresults=10))
    run('server.history(ratingKey)', lambda: server.history(ratingKey=keys['movie2'], maxresults=5))
    run('server.sessions()', lambda: server.sessions())
    run('server.transcodeSessions()', lambda: server.transcodeSessions())
    run('server.continueWatching()', lambda: server.continueWatching())
    run('server.systemAccounts()', lambda: server.systemAccounts())
    run('server.systemAccount(1)', lambda: server.systemAccount(1))
    run('server.systemDevices()', lambda: server.systemDevices())
    run('server.activities', lambda: server.activities)
    run('server.butlerTasks()', lambda: server.butlerTasks())
    run('server.settings.all()', lambda: server.settings.all())
    run('server.settings.groups()', lambda: server.settings.groups())
    run('server.settings.get(FriendlyName)', lambda: server.settings.get('FriendlyName'))
    run('server.clients()', lambda: server.clients())
    run('server.agents()', lambda: server.agents())
    run('server.agents(movie)', lambda: server.agents('movie'))
    run('server.browse()', lambda: server.browse())
    run('server.bandwidth()', lambda: server.bandwidth())
    run('server.resources()', lambda: server.resources())
    run('server.currentBackgroundProcess()', lambda: server.currentBackgroundProcess())
    run('server.isBrowsable', lambda: server.isBrowsable(server.browse()[0]) if server.browse() else None)
    run('server.url()', lambda: server.url('/library/metadata/1'))
    run('server.fetchItems(/library/sections)', lambda: server.fetchItems('/library/sections'))
    run('server.fetchItem(/library/metadata/movie)', lambda: server.fetchItem(f'/library/metadata/{keys["movie"]}'))
    run('server.fetchItem(int)', lambda: server.fetchItem(int(keys['movie'])))
    run('server.fetchItem(unknown 999999)', lambda: server.fetchItem(999999))
    run('server.playlist("nope")', lambda: server.playlist('nope'))
    run('library.sectionByID(unknown 99)', lambda: server.library.sectionByID(99))
    run('library.section("nope")', lambda: server.library.section('nope'))

    for sec in sections:
        p = f'sec[{sec.type}]'
        run(f'{p}.reload()', lambda: sec.reload())
        run(f'{p}.all()', lambda: sec.all(maxresults=10))
        run(f'{p}.all(sort=titleSort:desc)', lambda: sec.all(sort='titleSort:desc', maxresults=5))
        run(f'{p}.search(title="e")', lambda: sec.search(title='e', maxresults=5))
        run(f'{p}.search(filters year>>)', lambda: sec.search(filters={'year>>': 1900}, maxresults=5))
        run(f'{p}.search(unwatched=True)', lambda: sec.search(unwatched=True, maxresults=5))
        run(f'{p}.search(filters unwatched)', lambda: sec.search(filters={'unwatched': True}, maxresults=5))
        run(f'{p}.search(filters viewCount>>=0)', lambda: sec.search(filters={'viewCount>>=': 0}, maxresults=5))
        run(f'{p}.search(genre kw)', lambda: sec.search(genre=sec.listFilterChoices('genre')[0].title if sec.listFilterChoices('genre') else 'x', maxresults=5))
        run(f'{p}.search(sort=addedAt:desc)', lambda: sec.search(sort='addedAt:desc', maxresults=5))
        run(f'{p}.search(sort=random)', lambda: sec.search(sort='random', maxresults=5))
        run(f'{p}.search(sort bad)', lambda: sec.search(sort='nosuchsort', maxresults=5))
        run(f'{p}.search(libtype=movie)', lambda: sec.search(libtype='movie', maxresults=5))
        run(f'{p}.search(libtype=show)', lambda: sec.search(libtype='show', maxresults=5))
        run(f'{p}.search(libtype=season)', lambda: sec.search(libtype='season', maxresults=5))
        run(f'{p}.search(libtype=episode)', lambda: sec.search(libtype='episode', maxresults=5))
        run(f'{p}.search(libtype=collection)', lambda: sec.search(libtype='collection', maxresults=5))
        run(f'{p}.search(container_start/size)', lambda: sec.search(container_start=1, container_size=2, maxresults=2))
        run(f'{p}.hubs()', lambda: sec.hubs())
        run(f'{p}.managedHubs()', lambda: sec.managedHubs())
        run(f'{p}.hubSearch("e")', lambda: sec.hubSearch('e'))
        run(f'{p}.filterTypes()', lambda: sec.filterTypes())
        run(f'{p}.fieldTypes()', lambda: sec.fieldTypes())
        run(f'{p}.listFilters()', lambda: sec.listFilters())
        run(f'{p}.listFilters(episode)', lambda: sec.listFilters(libtype='episode'))
        run(f'{p}.listSorts()', lambda: sec.listSorts())
        run(f'{p}.listSorts(episode)', lambda: sec.listSorts(libtype='episode'))
        run(f'{p}.listFields()', lambda: sec.listFields())
        run(f'{p}.listOperators(string)', lambda: sec.listOperators('string'))
        run(f'{p}.listOperators(integer)', lambda: sec.listOperators('integer'))
        run(f'{p}.listFilterChoices(genre)', lambda: sec.listFilterChoices('genre'))
        run(f'{p}.listFilterChoices(year)', lambda: sec.listFilterChoices('year'))
        run(f'{p}.listFilterChoices(contentRating)', lambda: sec.listFilterChoices('contentRating'))
        run(f'{p}.listFilterChoices(resolution)', lambda: sec.listFilterChoices('resolution'))
        run(f'{p}.listFilterChoices(label)', lambda: sec.listFilterChoices('label'))
        run(f'{p}.listFilterChoices(collection)', lambda: sec.listFilterChoices('collection'))
        run(f'{p}.getFieldType(string)', lambda: sec.getFieldType('string'))
        run(f'{p}.getFilterType()', lambda: sec.getFilterType())
        run(f'{p}.collections()', lambda: sec.collections())
        run(f'{p}.playlists()', lambda: sec.playlists())
        run(f'{p}.onDeck()', lambda: sec.onDeck())
        run(f'{p}.recentlyAdded()', lambda: sec.recentlyAdded(maxresults=5))
        run(f'{p}.recentlyAdded(libtype)', lambda: sec.recentlyAdded(maxresults=5, libtype='episode' if sec.type == 'show' else 'movie'))
        run(f'{p}.continueWatching()', lambda: sec.continueWatching())
        run(f'{p}.agents()', lambda: sec.agents())
        run(f'{p}.settings()', lambda: sec.settings())
        run(f'{p}.timeline()', lambda: sec.timeline())
        run(f'{p}.totalSize', lambda: sec.totalSize)
        run(f'{p}.totalDuration', lambda: sec.totalDuration)
        run(f'{p}.totalStorage', lambda: sec.totalStorage)
        run(f'{p}.totalViewSize()', lambda: sec.totalViewSize())
        run(f'{p}.totalViewSize(episode)', lambda: sec.totalViewSize(libtype='episode'))
        run(f'{p}.folders()', lambda: sec.folders())
        run(f'{p}.firstCharacter()', lambda: sec.firstCharacter())
        run(f'{p}.history()', lambda: sec.history(maxresults=5))
        run(f'{p}.common(all)', lambda: sec.common(sec.all(maxresults=3)))
        run(f'{p}.get(first title)', lambda: sec.get(sec.all(maxresults=1)[0].title))
        run(f'{p}.getGuid(first)', lambda: sec.getGuid(sec.all(maxresults=1)[0].guid))
        run(f'{p}.get("nope")', lambda: sec.get('zzzzz-nope'))
        if sec.type == 'show':
            run(f'{p}.recentlyAddedShows()', lambda: sec.recentlyAddedShows(maxresults=3))
            run(f'{p}.recentlyAddedSeasons()', lambda: sec.recentlyAddedSeasons(maxresults=3))
            run(f'{p}.recentlyAddedEpisodes()', lambda: sec.recentlyAddedEpisodes(maxresults=3))
            run(f'{p}.searchShows()', lambda: sec.searchShows(maxresults=3))
            run(f'{p}.searchSeasons()', lambda: sec.searchSeasons(maxresults=3))
            run(f'{p}.searchEpisodes()', lambda: sec.searchEpisodes(maxresults=3))
        else:
            run(f'{p}.recentlyAddedMovies()', lambda: sec.recentlyAddedMovies(maxresults=3))
            run(f'{p}.searchMovies()', lambda: sec.searchMovies(maxresults=3))

    # ---- items ----
    def item_probes(label, obj):
        p = f'{label}'
        run(f'{p}.reload()', lambda: obj.reload())
        run(f'{p}.reload(checkFiles=0)', lambda: obj.reload(checkFiles=0))
        run(f'{p}.media', lambda: getattr(obj, 'media', None))
        run(f'{p}.media[0].parts', lambda: obj.media[0].parts if getattr(obj, 'media', None) else [])
        run(f'{p}.streams', lambda: obj.streams() if hasattr(obj, 'streams') else [s for m in getattr(obj, 'media', []) for pt in m.parts for s in pt.streams])
        run(f'{p}.videoStreams()', lambda: obj.videoStreams() if hasattr(obj, 'videoStreams') else None)
        run(f'{p}.audioStreams()', lambda: obj.audioStreams() if hasattr(obj, 'audioStreams') else None)
        run(f'{p}.subtitleStreams()', lambda: obj.subtitleStreams() if hasattr(obj, 'subtitleStreams') else None)
        run(f'{p}.iterParts()', lambda: list(obj.iterParts()) if hasattr(obj, 'iterParts') else None)
        run(f'{p}.locations', lambda: getattr(obj, 'locations', None))
        run(f'{p}.guids', lambda: getattr(obj, 'guids', None))
        run(f'{p}.fields', lambda: getattr(obj, 'fields', None))
        run(f'{p}.labels', lambda: getattr(obj, 'labels', None))
        run(f'{p}.collections', lambda: getattr(obj, 'collections', None))
        run(f'{p}.ratings', lambda: getattr(obj, 'ratings', None))
        run(f'{p}.images', lambda: getattr(obj, 'images', None))
        run(f'{p}.ultraBlurColors', lambda: getattr(obj, 'ultraBlurColors', None))
        run(f'{p}.similar', lambda: getattr(obj, 'similar', None))
        run(f'{p}.genres', lambda: getattr(obj, 'genres', None))
        run(f'{p}.roles', lambda: getattr(obj, 'roles', None))
        run(f'{p}.directors', lambda: getattr(obj, 'directors', None))
        run(f'{p}.writers', lambda: getattr(obj, 'writers', None))
        run(f'{p}.producers', lambda: getattr(obj, 'producers', None))
        run(f'{p}.countries', lambda: getattr(obj, 'countries', None))
        run(f'{p}.markers', lambda: getattr(obj, 'markers', None))
        run(f'{p}.chapters', lambda: getattr(obj, 'chapters', None))
        run(f'{p}.userRating', lambda: repr(getattr(obj, 'userRating', 'MISSING')))
        run(f'{p}.viewCount', lambda: repr(getattr(obj, 'viewCount', 'MISSING')))
        run(f'{p}.viewOffset', lambda: repr(getattr(obj, 'viewOffset', 'MISSING')))
        run(f'{p}.lastViewedAt', lambda: repr(getattr(obj, 'lastViewedAt', 'MISSING')))
        run(f'{p}.isPlayed', lambda: repr(obj.isPlayed))
        run(f'{p}.isFullObject()', lambda: repr(obj.isFullObject()))
        run(f'{p}.hasCreditsMarker', lambda: repr(getattr(obj, 'hasCreditsMarker', 'MISSING')))
        run(f'{p}.hasPreviewThumbnails', lambda: repr(getattr(obj, 'hasPreviewThumbnails', 'MISSING')))
        run(f'{p}.hasVoiceActivity', lambda: repr(getattr(obj, 'hasVoiceActivity', 'MISSING')))
        run(f'{p}.thumbUrl/artUrl/posterUrl', lambda: repr((obj.thumbUrl, obj.artUrl, obj.posterUrl))[:300])
        run(f'{p}.getWebURL()', lambda: obj.getWebURL())
        run(f'{p}.section()', lambda: obj.section())
        run(f'{p}.hubs()', lambda: obj.hubs())
        run(f'{p}.extras()', lambda: obj.extras())
        run(f'{p}.posters()', lambda: obj.posters())
        run(f'{p}.arts()', lambda: obj.arts())
        run(f'{p}.logos()', lambda: obj.logos())
        run(f'{p}.squareArts()', lambda: obj.squareArts())
        run(f'{p}.themes()', lambda: obj.themes() if hasattr(obj, 'themes') else None)
        run(f'{p}.matches()', lambda: obj.matches() if hasattr(obj, 'matches') else None)
        run(f'{p}.matches(title=)', lambda: obj.matches(title='Tears of Steel', year=2012) if hasattr(obj, 'matches') else None)
        run(f'{p}.history()', lambda: obj.history(maxresults=5))
        run(f'{p}.preferences()', lambda: obj.preferences() if hasattr(obj, 'preferences') else None)
        run(f'{p}.reviews()', lambda: obj.reviews() if hasattr(obj, 'reviews') else None)
        run(f'{p}.editions()', lambda: obj.editions() if hasattr(obj, 'editions') else None)
        run(f'{p}.streamingServices()', lambda: obj.streamingServices() if hasattr(obj, 'streamingServices') else None)
        run(f'{p}.augmentation()', lambda: obj.augmentation() if hasattr(obj, 'augmentation') else None)
        run(f'{p}.getStreamURL()', lambda: obj.getStreamURL() if hasattr(obj, 'getStreamURL') else None)
        run(f'{p}.getStreamURL(videoResolution)', lambda: obj.getStreamURL(videoResolution='800x600') if hasattr(obj, 'getStreamURL') else None)
        run(f'{p}.metadataDirectory', lambda: getattr(obj, 'metadataDirectory', None))
        run(f'{p}.sessions()', lambda: obj.sessions() if hasattr(obj, 'sessions') else None)
        run(f'{p}.transcodeSessions()', lambda: obj.transcodeSessions() if hasattr(obj, 'transcodeSessions') else None)
        run(f'{p}.listAttrs()', lambda: repr(obj.listAttrs(obj._data, 'ratingKey')) if hasattr(obj, 'listAttrs') else None)
        if hasattr(obj, 'onDeck'):
            run(f'{p}.onDeck()', lambda: obj.onDeck())
        if hasattr(obj, 'seasons'):
            run(f'{p}.seasons()', lambda: obj.seasons())
            run(f'{p}.season(1)', lambda: obj.season(1))
            run(f'{p}.season(title)', lambda: obj.season(obj.seasons()[0].title))
            run(f'{p}.episodes()', lambda: obj.episodes())
            run(f'{p}.episode(season=1, episode=1)', lambda: obj.episode(season=1, episode=1))
            run(f'{p}.episode(title)', lambda: obj.episode(obj.episodes()[0].title))
            run(f'{p}.get(title)', lambda: obj.get(obj.episodes()[0].title))
            run(f'{p}.watched()', lambda: obj.watched())
            run(f'{p}.unwatched()', lambda: obj.unwatched())
        if label.startswith('season'):
            run(f'{p}.show()', lambda: obj.show())
            run(f'{p}.episodes()', lambda: obj.episodes())
            run(f'{p}.episode(1)', lambda: obj.episode(1))
            run(f'{p}.watched()', lambda: obj.watched())
            run(f'{p}.unwatched()', lambda: obj.unwatched())
            run(f'{p}.seasonNumber', lambda: repr(obj.seasonNumber))
        if label.startswith('episode'):
            run(f'{p}.show()', lambda: obj.show())
            run(f'{p}.season()', lambda: obj.season())
            run(f'{p}.seasonEpisode', lambda: repr(obj.seasonEpisode))
            run(f'{p}.hasIntroMarker', lambda: repr(obj.hasIntroMarker))
            run(f'{p}.hasCommercialMarker', lambda: repr(obj.hasCommercialMarker))
            run(f'{p}.episodeNumber', lambda: repr(obj.episodeNumber))
            run(f'{p}.seasonNumber', lambda: repr(obj.seasonNumber))

    movie = run('fetchItem(movie)', lambda: server.fetchItem(keys['movie']))
    if movie is not None:
        item_probes('movie', movie)
    movie2 = run('fetchItem(movie2 played)', lambda: server.fetchItem(keys['movie2']))
    if movie2 is not None:
        run('movie2.reload()', lambda: movie2.reload())
        run('movie2.userRating', lambda: repr(movie2.userRating))
        run('movie2.viewCount', lambda: repr(movie2.viewCount))
        run('movie2.viewOffset', lambda: repr(movie2.viewOffset))
        run('movie2.lastViewedAt', lambda: repr(movie2.lastViewedAt))
        run('movie2.lastRatedAt', lambda: repr(getattr(movie2, 'lastRatedAt', 'MISSING')))
        run('movie2.isPlayed', lambda: repr(movie2.isPlayed))
        run('movie2.history()', lambda: movie2.history(maxresults=5))
    movie3 = run('fetchItem(movie3)', lambda: server.fetchItem(keys['movie3']))
    if movie3 is not None:
        run('movie3.reload()', lambda: movie3.reload())
        run('movie3.viewOffset', lambda: repr(movie3.viewOffset))
        run('movie3.lastViewedAt', lambda: repr(movie3.lastViewedAt))
    show = run('fetchItem(show)', lambda: server.fetchItem(keys['show']))
    if show is not None:
        item_probes('show', show)
        seasons = run('show.seasons() [for keys]', lambda: show.seasons()) or []
        season = None
        if keys.get('season'):
            season = run('fetchItem(season)', lambda: server.fetchItem(keys['season']))
        elif seasons:
            season = run('fetchItem(seasons[0])', lambda: server.fetchItem(seasons[0].ratingKey))
        if season is not None:
            item_probes('season', season)
            eps = run('season.episodes() [for keys]', lambda: season.episodes()) or []
            episode = None
            if keys.get('episode'):
                episode = run('fetchItem(episode)', lambda: server.fetchItem(keys['episode']))
            elif eps:
                episode = run('fetchItem(eps[0])', lambda: server.fetchItem(eps[0].ratingKey))
            if episode is not None:
                item_probes('episode', episode)
    if keys.get('episode2'):
        ep2 = run('fetchItem(episode2)', lambda: server.fetchItem(keys['episode2']))
        if ep2 is not None:
            run('episode2.reload()', lambda: ep2.reload())
            run('episode2.viewOffset', lambda: repr(ep2.viewOffset))
            run('episode2.viewCount', lambda: repr(ep2.viewCount))
            run('episode2.lastViewedAt', lambda: repr(ep2.lastViewedAt))

    # collections / playlists detail
    for sec in sections:
        cols = run(f'sec[{sec.type}].collections() [detail]', lambda: sec.collections()) or []
        if cols:
            c = cols[0]
            run(f'collection[{sec.type}].reload()', lambda: c.reload())
            run(f'collection[{sec.type}].items()', lambda: c.items())
            run(f'collection[{sec.type}].item(first)', lambda: c.item(c.items()[0].title) if c.items() else None)
            run(f'collection[{sec.type}].get(first)', lambda: c.get(c.items()[0].title) if c.items() else None)
            run(f'collection[{sec.type}].hubs()', lambda: c.hubs())
            run(f'collection[{sec.type}].posters()', lambda: c.posters())
            run(f'collection[{sec.type}].arts()', lambda: c.arts())
            run(f'collection[{sec.type}].filters()', lambda: c.filters())
            run(f'collection[{sec.type}].section()', lambda: c.section())
            run(f'collection[{sec.type}].history()', lambda: c.history(maxresults=5))
            run(f'collection[{sec.type}].preferences()', lambda: c.preferences())
            run(f'collection[{sec.type}].props', lambda: repr((c.smart, c.collectionMode, c.collectionSort, c.contentRating, c.childCount, c.isVideo, c.listType, c.metadataType, c.subtype, c.collectionFilterBasedOnUser, c.collectionPublished))[:400])
            run(f'collection[{sec.type}].labels', lambda: c.labels)
            run(f'collection[{sec.type}].fields', lambda: c.fields)
            run(f'collection[{sec.type}].images', lambda: c.images)
            run(f'collection[{sec.type}].ultraBlurColors', lambda: c.ultraBlurColors)
            run(f'collection[{sec.type}].sectionTriple', lambda: repr((c.librarySectionID, c.librarySectionKey, c.librarySectionTitle)))
    pls = run('server.playlists() [detail]', lambda: server.playlists()) or []
    if pls:
        pl = pls[0]
        run('playlist.reload()', lambda: pl.reload())
        run('playlist.items()', lambda: pl.items())
        run('playlist.item(first)', lambda: pl.item(pl.items()[0].title) if pl.items() else None)
        run('playlist.get(first)', lambda: pl.get(pl.items()[0].title) if pl.items() else None)
        run('playlist.posters()', lambda: pl.posters())
        run('playlist.arts()', lambda: pl.arts())
        run('playlist.filters()', lambda: pl.filters())
        run('playlist.section()', lambda: pl.section())
        run('playlist.history()', lambda: pl.history(maxresults=5))
        run('playlist.props', lambda: repr((pl.smart, pl.playlistType, pl.duration, pl.leafCount, pl.isVideo, pl.metadataType, pl.thumb, pl.composite, pl.radio, pl.icon, pl.content))[:500])
        run('playlist.iterParts()', lambda: list(pl.iterParts()))
        run('playlist.streams', lambda: pl.videoStreams())
        run('playlist.getStreamURL()', lambda: pl.getStreamURL())
        run('server.playlist(title)', lambda: server.playlist(pl.title))
        run('playlist.fields', lambda: pl.fields)
        run('playlist.images', lambda: pl.images)

    json.dump(dict(server=name, results=results), open(f'out_{name}.json', 'w'), indent=1)
    print('done', name, len(results), 'probes', len(sess.log), 'requests', file=sys.stderr)


if __name__ == '__main__':
    main()
