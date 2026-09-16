from ..models import Candidate
class MetaHubProvider:
    name='metahub'
    def candidates(self, imdb_id, art_type):
        if not imdb_id: return []
        return [Candidate(provider=self.name,url=f'https://images.metahub.space/{art_type}/medium/{imdb_id}/img',kind=art_type,label='MetaHub/IMDb')]
