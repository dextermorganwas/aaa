from ..models import Candidate


class MetaHubProvider:
    name = 'metahub'

    def candidates(self, imdb_id, art_type):
        if not imdb_id:
            return []
        endpoint_type = 'background' if art_type == 'backdrop' else art_type
        return [Candidate(
            provider=self.name,
            url=f'https://images.metahub.space/{endpoint_type}/medium/{imdb_id}/img',
            kind=art_type,
            label='MetaHub/IMDb',
        )]
