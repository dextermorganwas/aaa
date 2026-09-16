from ..models import Candidate


class MetaHubProvider:
    name = 'metahub'

    def candidates(self, imdb_id, art_type):
        if not imdb_id:
            return []

        # MetaHub calls this asset type "background" even though our public
        # resolver/admin API calls it "backdrop". Using /backdrop here returns
        # the wrong endpoint. Keep the app-facing art_type unchanged.
        endpoint_type = 'background' if art_type == 'backdrop' else art_type
        return [
            Candidate(
                provider=self.name,
                url=f'https://images.metahub.space/{endpoint_type}/medium/{imdb_id}/img',
                kind=art_type,
                label='MetaHub/IMDb',
            )
        ]
