from app.util import parse_art_request

def test_parse_aiometadata_url():
    kind, ids = parse_art_request('poster','tmdb:movie:550&imdb:tt0137523&tvdb:.jpg')
    assert kind=='movie'
    assert ids['tmdb_id']=='550'
    assert ids['imdb_id']=='tt0137523'
    assert ids['tvdb_id'] is None

def test_parse_series():
    kind, ids = parse_art_request('backdrop','tmdb:series:1399&imdb:tt0944947&tvdb:121361.jpg')
    assert kind=='series'; assert ids['tmdb_id']=='1399'; assert ids['tvdb_id']=='121361'
