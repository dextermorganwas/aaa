from app.providers.metahub import MetaHubProvider


def test_metahub_uses_background_endpoint_for_backdrop():
    c = MetaHubProvider().candidates('tt2072233', 'backdrop')[0]
    assert c.kind == 'backdrop'
    assert c.url == 'https://images.metahub.space/background/medium/tt2072233/img'


def test_metahub_keeps_poster_endpoint():
    c = MetaHubProvider().candidates('tt2072233', 'poster')[0]
    assert c.url == 'https://images.metahub.space/poster/medium/tt2072233/img'
