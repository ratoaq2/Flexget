from datetime import datetime, timedelta
import logging
from urllib2 import URLError
import yaml
import os
import posixpath
from sqlalchemy import Table, Column, Integer, Float, String, Unicode, Boolean, DateTime, func
from sqlalchemy.schema import ForeignKey
from sqlalchemy.orm import relation, joinedload
from flexget.utils.titles import MovieParser
from flexget.utils.tools import urlopener
from flexget.utils.database import text_date_synonym, year_property, with_session
from flexget.manager import Base, Session
from flexget.plugin import register_plugin

log = logging.getLogger('api_tmdb')

# This is a FlexGet API key
api_key = 'bdfc018dbdb7c243dc7cb1454ff74b95'
lang = 'en'
server = 'http://api.themoviedb.org'


# association tables
genres_table = Table('tmdb_movie_genres', Base.metadata,
    Column('movie_id', Integer, ForeignKey('tmdb_movies.id')),
    Column('genre_id', Integer, ForeignKey('tmdb_genres.id')))


class TMDBContainer(object):
    """Base class for TMDb objects"""

    def __init__(self, init_dict=None):
        if isinstance(init_dict, dict):
            self.update_from_dict(init_dict)

    def update_from_dict(self, update_dict):
        """Populates any simple (string or number) attributes from a dict"""
        for col in self.__table__.columns:
            if isinstance(update_dict.get(col.name), (basestring, int, float)):
                setattr(self, col.name, update_dict[col.name])


class TMDBMovie(TMDBContainer, Base):

    __tablename__ = 'tmdb_movies'

    id = Column(Integer, primary_key=True, autoincrement=False, nullable=False)
    updated = Column(DateTime, default=datetime.now(), nullable=False)
    popularity = Column(Integer)
    translated = Column(Boolean)
    adult = Column(Boolean)
    language = Column(String)
    original_name = Column(Unicode)
    name = Column(Unicode)
    alternative_name = Column(Unicode)
    movie_type = Column(String)
    imdb_id = Column(String)
    url = Column(String)
    votes = Column(Integer)
    rating = Column(Float)
    certification = Column(String)
    overview = Column(Unicode)
    _released = Column('released', DateTime)
    released = text_date_synonym('_released')
    year = year_property('released')
    posters = relation('TMDBPoster', backref='movie', cascade='all, delete, delete-orphan')
    genres = relation('TMDBGenre', secondary=genres_table, backref='movies')


class TMDBGenre(TMDBContainer, Base):

    __tablename__ = 'tmdb_genres'

    id = Column(Integer, primary_key=True, autoincrement=False)
    name = Column(String, nullable=False)


class TMDBPoster(TMDBContainer, Base):

    __tablename__ = 'tmdb_posters'

    db_id = Column(Integer, primary_key=True)
    movie_id = Column(Integer, ForeignKey('tmdb_movies.id'))
    size = Column(String)
    url = Column(String)
    id = Column(String)
    type = Column(String)
    file = Column(Unicode)

    def get_file(self, only_cached=False):
        """Makes sure the poster is downloaded to the local cache (in userstatic folder) and
        returns the path split into a list of directory and file components"""
        from flexget.manager import manager
        base_dir = os.path.join(manager.config_base, 'userstatic')
        if self.file and os.path.isfile(os.path.join(base_dir, self.file)):
            return self.file.split(os.sep)
        elif only_cached:
            return
        # If we don't already have a local copy, download one.
        log.debug('Downloading poster %s' % self.url)
        dirname = os.path.join('tmdb', 'posters', str(self.movie_id))
        # Create folders if they don't exist
        fullpath = os.path.join(base_dir, dirname)
        if not os.path.isdir(fullpath):
            os.makedirs(fullpath)
        filename = os.path.join(dirname, posixpath.basename(self.url))
        thefile = file(os.path.join(base_dir, filename), 'wb')
        thefile.write(urlopener(self.url, log).read())
        self.file = filename
        # If we are detached from a session, update the db
        if not Session.object_session(self):
            session = Session()
            poster = session.query(TMDBPoster).filter(TMDBPoster.db_id == self.db_id).first()
            if poster:
                poster.file = filename
                session.commit()
            session.close()
        return filename.split(os.sep)


class TMDBSearchResult(Base):

    __tablename__ = 'tmdb_search_results'

    id = Column(Integer, primary_key=True)
    search = Column(Unicode, nullable=False)
    movie_id = Column(Integer, ForeignKey('tmdb_movies.id'), nullable=True)
    movie = relation(TMDBMovie, backref='search_strings')


class ApiTmdb(object):
    """Does lookups to TMDb and provides movie information. Caches lookups."""

    @with_session
    def lookup(self, title=None, year=None, tmdb_id=None, imdb_id=None, smart_match=None, only_cached=False, session=None):
        if not (tmdb_id or imdb_id or title) and smart_match:
            # If smart_match was specified, and we don't have more specific criteria, parse it into a title and year
            title_parser = MovieParser()
            title_parser.parse(smart_match)
            title = title_parser.name
            year = title_parser.year

        if title:
            search_string = title.lower()
            if year:
                search_string = '%s %s' % (search_string, year)
        elif not (tmdb_id or imdb_id):
            raise LookupError('No criteria specified for tmdb lookup')
        log.debug('Looking up tmdb information for %r' % {'title': title, 'tmdb_id': tmdb_id, 'imdb_id': imdb_id})

        movie = None

        def id_str():
            return '<title=%s,tmdb_id=%s,imdb_id=%s>' % (title, tmdb_id, imdb_id)
        if tmdb_id:
            movie = session.query(TMDBMovie).filter(TMDBMovie.id == tmdb_id).first()
        if not movie and imdb_id:
            movie = session.query(TMDBMovie).filter(TMDBMovie.imdb_id == imdb_id).first()
        if not movie and title:
            movie_filter = session.query(TMDBMovie).filter(func.lower(TMDBMovie.name) == title.lower())
            if year:
                movie_filter = movie_filter.filter(TMDBMovie.year == year)
            movie = movie_filter.first()
            if not movie:
                found = session.query(TMDBSearchResult). \
                        filter(func.lower(TMDBSearchResult.search) == search_string).first()
                if found and found.movie:
                    movie = found.movie
        if movie:
            # Movie found in cache, check if cache has expired.
            refresh_time = timedelta(days=2)
            if movie.released:
                if movie.released > datetime.now() - timedelta(days=7):
                    # Movie is less than a week old, expire after 1 day
                    refresh_time = timedelta(days=1)
                else:
                    age_in_years = (datetime.now() - movie.released).days / 365
                    refresh_time += timedelta(days=age_in_years * 5)
            if movie.updated < datetime.now() - refresh_time and not only_cached:
                log.debug('Cache has expired for %s, attempting to refresh from TMDb.' % id_str())
                try:
                    self.get_movie_details(movie, session)
                except URLError:
                    log.error('Error refreshing movie details from TMDb, cached info being used.')
            else:
                log.debug('Movie %s information restored from cache.' % id_str())
        else:
            if only_cached:
                raise LookupError('Movie %s not found from cache' % id_str())
            # There was no movie found in the cache, do a lookup from tmdb
            log.debug('Movie %s not found in cache, looking up from tmdb.' % id_str())
            try:
                if tmdb_id or imdb_id:
                    movie = TMDBMovie()
                    movie.id = tmdb_id
                    movie.imdb_id = imdb_id
                    self.get_movie_details(movie, session)
                    if movie.name:
                        session.merge(movie)
                elif title:
                    result = get_first_result('search', search_string)
                    if result:
                        movie = session.query(TMDBMovie).filter(TMDBMovie.id == result['id']).first()
                        if not movie:
                            movie = TMDBMovie(result)
                            self.get_movie_details(movie, session)
                            session.merge(movie)
                        if title.lower() != movie.name.lower():
                            session.merge(TMDBSearchResult(search=search_string, movie=movie))
            except URLError:
                raise LookupError('Error looking up movie from TMDb')

        if not movie:
            raise LookupError('No results found from tmdb for %s' % id_str())
        else:
            # Access attributes to force the relationships to eager load before we detach from session
            movie.genres
            movie.posters
            return movie

    def get_movie_details(self, movie, session):
        """Populate details for this :movie: from TMDb"""

        if not movie.id and movie.imdb_id:
            # If we have an imdb_id, do a lookup for tmdb id
            result = get_first_result('imdbLookup', movie.imdb_id)
            if result:
                movie.update_from_dict(result)
        if not movie.id:
            log.error('Cannot get tmdb details without tmdb id')
            return
        result = get_first_result('getInfo', movie.id)
        if result:
            movie.update_from_dict(result)
            posters = result.get('posters')
            if posters:
                # Add any posters we don't already have
                # TODO: There are quite a few posters per movie, do we need to cache them all?
                poster_urls = [p.url for p in movie.posters]
                for item in posters:
                    if item.get('image') and item['image']['url'] not in poster_urls:
                        movie.posters.append(TMDBPoster(item['image']))
            genres = result.get('genres')
            if genres:
                for genre in genres:
                    if not genre.get('id'):
                        continue
                    db_genre = session.query(TMDBGenre).filter(TMDBGenre.id == genre['id']).first()
                    if not db_genre:
                        db_genre = TMDBGenre(genre)
                    if db_genre not in movie.genres:
                        movie.genres.append(db_genre)
            movie.updated = datetime.now()


def get_first_result(tmdb_function, value):
    if isinstance(value, basestring):
        import urllib
        value = urllib.quote_plus(value)
    url = '%s/2.1/Movie.%s/%s/yaml/%s/%s' % (server, tmdb_function, lang, api_key, value)
    from urllib2 import URLError
    try:
        data = urlopener(url, log)
    except URLError, e:
        log.warning('Request failed %s' % url)
        return

    result = yaml.load(data)
    # Make sure there is a valid result to return
    if isinstance(result, list) and len(result):
        result = result[0]
        if isinstance(result, dict) and result.get('id'):
            return result

register_plugin(ApiTmdb, 'api_tmdb')
