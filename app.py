from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import time
import re
import os

app = Flask(__name__)
CORS(app)

cache = {}
CACHE_SECONDS = 25

# The ECB NV Play customer ID - same for all ECB/Play-Cricket matches
ECB_CUSTOMER_ID = '6cb09e4b-e702-4942-8760-659da2d1b3ff'
MATCHLIST_URL = (
    'https://w-api.ecb.nvplay.net/api/matchlist/filter'
    '?customerid={cid}&addFilters=false&advanced=true&currentSeason=false'
    '&days=1&competitionId=&start=&end=&teams=&matchTypes=&videoOnly=false'
    '&page=0&showFixtures=true&showLive=true&showResults=true'
    '&completedResultsOnly=false&maxResults=200'
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Referer': 'https://live.nvplay.com/',
    'Origin': 'https://live.nvplay.com',
}

def extract_match_id(url):
    """Extract NV Play match GUID from various URL formats"""
    # From NV Play URL: #m63058ade-cda2-49b5-96fa-dd5b03d261fa
    m = re.search(r'#m([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url)
    if m:
        return m.group(1)
    # Bare GUID anywhere in URL
    m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url)
    if m:
        return m.group(1)
    return None

def fetch_nvplay_scorecard(match_id):
    # Try primary endpoint
    urls = [
        f'https://w-api.ecb.nvplay.net/api/scorecard/{match_id}?idType=nvplay&customerId={ECB_CUSTOMER_ID}&stats=true&commentary=false',
        f'https://w-api.ecb.nvplay.net/api/scorecard/{match_id}?customerId={ECB_CUSTOMER_ID}&stats=true',
        f'https://w-api.ecb.nvplay.net/api/match/{match_id}/scorecard?customerId={ECB_CUSTOMER_ID}',
    ]
    last_error = None
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            last_error = f'HTTP {resp.status_code} from {url}'
        except Exception as e:
            last_error = str(e)
    raise Exception(last_error or 'All scorecard endpoints failed')

def parse_nvplay(data):
    match = data.get('Match', {})
    innings_raw = data.get('Innings', [])

    result = {
        'match_title': match.get('MatchTitle', ''),
        'competition': match.get('CompetitionName', ''),
        'venue': match.get('VenueName', '') + (', ' + match.get('VenueCity', '') if match.get('VenueCity') else ''),
        'date': match.get('StartDateFormatted', ''),
        'status': match.get('MatchStatus', ''),
        'result': match.get('Result') or None,
        'toss': match.get('TossWinnerDescription', ''),
        'team1': match.get('Team1Name', ''),
        'team2': match.get('Team2Name', ''),
        'team1_score': match.get('Team1Scores', ''),
        'team2_score': match.get('Team2Scores', ''),
        'match_situation': match.get('MatchSituationScore', ''),
        'is_live': match.get('IsLive', False),
        'innings': [],
        'error': None
    }

    for inn in innings_raw:
        batting_team = inn.get('BattingTeamName', '')
        score_simple = inn.get('ScoreSimple', '')
        total_overs = inn.get('TotalOvers', '')
        declared = inn.get('Declared', False)
        extras = inn.get('TotalExtras', '')
        byes = inn.get('TotalByes', 0)
        leg_byes = inn.get('TotalLegByes', 0)
        wides = inn.get('TotalWides', 0) if 'TotalWides' in inn else 0
        no_balls = inn.get('TotalNoBalls', 0) if 'TotalNoBalls' in inn else 0

        extras_str = f"{extras} (b{byes} lb{leg_byes} w{wides} nb{no_balls})" if extras else ''

        batsmen = []
        for b in inn.get('BattingCard', []):
            if b.get('IsSummary'):
                continue
            status = 'not out'
            if b.get('IsDismissed'):
                status = b.get('HowOut', 'out')
            elif b.get('IsFirstActive') or b.get('IsSecondActive'):
                status = 'batting'
            batsmen.append({
                'name': b.get('PlayerName', ''),
                'status': status,
                'runs': b.get('Runs'),
                'balls': b.get('Balls'),
                'fours': b.get('Fours'),
                'sixes': b.get('Sixes'),
                'sr': b.get('StrikeRate', ''),
                'is_striker': b.get('IsStriker', False),
            })

        bowlers = []
        for b in inn.get('BowlingCard', []):
            if b.get('IsSummary'):
                continue
            overs_str = str(b.get('Overs', ''))
            balls = b.get('Balls', 0)
            if balls:
                overs_str = f"{b.get('Overs', 0)}.{balls}"
            bowlers.append({
                'name': b.get('PlayerName', ''),
                'overs': overs_str,
                'maidens': b.get('Maidens'),
                'runs': b.get('Runs'),
                'wickets': b.get('Wickets'),
                'econ': b.get('Economy', ''),
                'is_bowling': b.get('IsCurrentlyBowling', False),
            })

        fow_list = inn.get('FallOfWickets', [])
        fow_str = ', '.join([
            f"{f.get('Runs')}-{f.get('Wickets')} ({f.get('PlayerName')}, {f.get('Overs')})"
            for f in fow_list
        ]) if fow_list else ''

        result['innings'].append({
            'batting_team': batting_team,
            'score': score_simple,
            'overs': total_overs,
            'declared': declared,
            'batsmen': batsmen,
            'bowlers': bowlers,
            'fall_of_wickets': fow_str,
            'extras': extras_str,
        })

    if not result['innings']:
        result['error'] = 'Match has not started yet or no scoring data available.'

    return result

def get_cached_or_fetch(match_id):
    now = time.time()
    if match_id in cache:
        entry = cache[match_id]
        if now - entry['timestamp'] < CACHE_SECONDS:
            return entry['data']
    print(f"[proxy] Fetching match: {match_id}")
    try:
        raw = fetch_nvplay_scorecard(match_id)
        data = parse_nvplay(raw)
    except Exception as e:
        data = {
            'error': f'Could not load scorecard for match {match_id}: {str(e)}',
            'innings': [], 'match_title': '', 'status': 'Error',
            'team1': '', 'team2': '', 'team1_score': '', 'team2_score': ''
        }
    cache[match_id] = {'data': data, 'timestamp': now}
    return data

@app.route('/debug-matches')
def debug_matches():
    try:
        url = MATCHLIST_URL.format(cid=ECB_CUSTOMER_ID)
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Return raw structure so we can see keys
        if isinstance(data, list):
            sample = data[:2]
            return jsonify({'type': 'list', 'total': len(data), 'sample_keys': list(sample[0].keys()) if sample else [], 'sample': sample})
        else:
            return jsonify({'type': 'dict', 'keys': list(data.keys()), 'raw': data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/matches')
def matches():
    try:
        url = MATCHLIST_URL.format(cid=ECB_CUSTOMER_ID)
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        match_list = []
        # API returns separate Live, Fixtures, Results arrays
        sections = [
            ('live', data.get('Live', []) or []),
            ('fixture', data.get('Fixtures', []) or []),
            ('result', data.get('Results', []) or []),
        ]
        for section_type, items in sections:
            for m in items:
                match_list.append({
                    'id': m.get('MatchId', ''),
                    'title': m.get('MatchShortTitle') or m.get('MatchTitle', ''),
                    'team1': m.get('Team1Name', ''),
                    'team2': m.get('Team2Name', ''),
                    'competition': m.get('CompetitionName', ''),
                    'venue': m.get('VenueName', ''),
                    'venue_city': m.get('VenueCity', ''),
                    'status': m.get('MatchStatus', '') or section_type.title(),
                    'is_live': m.get('IsLive', False),
                    'is_in_play': m.get('IsInPlay', False),
                    'is_complete': m.get('IsComplete', False),
                    'score1': m.get('Team1Scores', '') or '',
                    'score2': m.get('Team2Scores', '') or '',
                    'match_type': m.get('MatchType', ''),
                    'start': m.get('StartDateFormatted', ''),
                    'situation': m.get('MatchSituationNoScore', '') or m.get('MatchSituation', '') or '',
                    'section': section_type,
                })

        return jsonify({'matches': match_list, 'total': len(match_list)})
    except Exception as e:
        return jsonify({'error': str(e), 'matches': []}), 500


def score():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    match_id = extract_match_id(url)
    if not match_id:
        return jsonify({'error': 'Could not find a match ID in that URL. Please use a URL from live.nvplay.com/ecb that contains a match ID like #m63058ade-...'}), 400

    data = get_cached_or_fetch(match_id)
    return jsonify(data)

@app.route('/scoreboard')
def scoreboard():
    try:
        with open('cricket_scoreboard.html', 'r') as f:
            return f.read(), 200, {'Content-Type': 'text/html'}
    except:
        return 'Scoreboard file not found. Upload cricket_scoreboard.html to the same folder as app.py', 404


def health():
    return jsonify({'status': 'ok'})

@app.route('/')
def index():
    return '''<html><body style="font-family:sans-serif;padding:30px;background:#0a1a0f;color:#f0ead8">
    <h2>&#x1F3CF; Cricket Proxy</h2>
    <p style="color:#7de0a0">&#10003; Running OK</p>
    <p><a href="/scoreboard" style="color:#28a84a;font-size:18px;font-weight:bold">&#x25B6; Open Scoreboard</a></p>
    </body></html>'''

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
