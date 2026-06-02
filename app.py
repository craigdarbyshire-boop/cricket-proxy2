from flask import Flask, jsonify, request
from flask_cors import CORS
from bs4 import BeautifulSoup
import requests
import time
import re
import os

app = Flask(__name__)
CORS(app)

cache = {}
CACHE_SECONDS = 25

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://www.play-cricket.com/',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

def fetch_page(url):
    session = requests.Session()
    # First hit the homepage to get cookies
    try:
        session.get('https://www.play-cricket.com/', headers=HEADERS, timeout=10)
    except:
        pass
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text

def safe_int(val):
    try:
        return int(str(val).strip())
    except:
        return None

def parse_scorecard(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    result = {
        'match_title': '',
        'competition': '',
        'venue': '',
        'date': '',
        'status': 'Unknown',
        'result': None,
        'innings': [],
        'url': url,
        'error': None,
        'raw_snippet': ''
    }

    # Grab a text snippet for debugging
    body_text = soup.get_text(separator=' ', strip=True)
    result['raw_snippet'] = body_text[:500]

    for sel in ['h1.match-title','h1','.match-header h1','.page-title']:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            result['match_title'] = el.get_text(strip=True)
            break

    for el in soup.select('.match-details li,.match-info li,.fixture-info li,.detail-item'):
        txt = el.get_text(strip=True)
        low = txt.lower()
        if any(x in low for x in ['league','cup','division','competition','trophy']):
            result['competition'] = txt
        elif any(x in low for x in ['ground','venue','at ']):
            result['venue'] = txt
        elif re.search(r'\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}', txt) and not result['date']:
            result['date'] = txt

    for sel in ['.match-result','.result-banner','.match-status','.result']:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            result['result'] = el.get_text(strip=True)
            result['status'] = 'Complete'
            break

    innings_containers = soup.select('.scorecard-innings,.innings-scorecard,.innings,[class*="innings"]')
    if not innings_containers:
        innings_containers = soup.select('table.batting,.batting-scorecard')

    for inn_el in innings_containers:
        inn = parse_innings(inn_el)
        if inn:
            result['innings'].append(inn)

    if not result['innings']:
        result['innings'] = parse_by_tables(soup)

    if result['innings'] and not result['result']:
        result['status'] = 'In Progress'
    elif not result['innings']:
        result['status'] = 'No data yet'
        result['error'] = (
            'Scorecard not yet available. This may mean:\n'
            '1. The match has not started yet\n'
            '2. Scoring has not begun on the Play-Cricket app\n'
            '3. Play-Cricket is blocking the request\n\n'
            'Page preview: ' + body_text[:200]
        )

    return result

def parse_innings(inn_el):
    inn = {
        'batting_team':'','score':'','overs':'','declared':False,
        'batsmen':[],'bowlers':[],'fall_of_wickets':'','extras':''
    }
    for sel in ['h2','h3','h4','.innings-title','.team-name','caption']:
        el = inn_el.select_one(sel)
        if el and el.get_text(strip=True):
            inn['batting_team'] = el.get_text(strip=True)
            break

    score_el = inn_el.select_one('.total,.innings-total,.score,.runs-wickets')
    if score_el:
        txt = score_el.get_text(strip=True)
        m = re.search(r'(\d+)[\/\-](\d+)', txt)
        if m:
            inn['score'] = f"{m.group(1)}/{m.group(2)}"
        ov = re.search(r'\(([0-9.]+)\s*ov', txt, re.I)
        if ov:
            inn['overs'] = ov.group(1)
        if 'dec' in txt.lower():
            inn['declared'] = True

    bat_table = inn_el.select_one('table.batting,table[class*="bat"]')
    if not bat_table:
        for tbl in inn_el.select('table'):
            hdrs = [th.get_text(strip=True).lower() for th in tbl.select('th')]
            if any(h in hdrs for h in ['runs','r','batsman','batter']):
                bat_table = tbl
                break

    if bat_table:
        for row in bat_table.select('tr'):
            cells = [td.get_text(strip=True) for td in row.select('td')]
            if len(cells) >= 3 and cells[0] and cells[0].lower() not in ['batsman','batter','player','name']:
                status = cells[1] if len(cells) > 1 else ''
                if 'not out' in status.lower():
                    status = 'not out'
                elif not status.strip():
                    status = 'batting'
                inn['batsmen'].append({
                    'name': cells[0], 'status': status,
                    'runs': safe_int(cells[2]) if len(cells)>2 else None,
                    'balls': safe_int(cells[3]) if len(cells)>3 else None,
                    'fours': safe_int(cells[4]) if len(cells)>4 else None,
                    'sixes': safe_int(cells[5]) if len(cells)>5 else None,
                    'sr': cells[6] if len(cells)>6 else '',
                })

    bowl_table = inn_el.select_one('table.bowling,table[class*="bowl"]')
    if not bowl_table:
        for tbl in inn_el.select('table'):
            hdrs = [th.get_text(strip=True).lower() for th in tbl.select('th')]
            if any(h in hdrs for h in ['wkts','wickets','w','bowler']):
                bowl_table = tbl
                break

    if bowl_table:
        for row in bowl_table.select('tr'):
            cells = [td.get_text(strip=True) for td in row.select('td')]
            if len(cells) >= 4 and cells[0] and cells[0].lower() not in ['bowler','player','name']:
                inn['bowlers'].append({
                    'name': cells[0],
                    'overs': cells[1] if len(cells)>1 else '',
                    'maidens': safe_int(cells[2]) if len(cells)>2 else None,
                    'runs': safe_int(cells[3]) if len(cells)>3 else None,
                    'wickets': safe_int(cells[4]) if len(cells)>4 else None,
                    'econ': cells[5] if len(cells)>5 else '',
                })

    for el in inn_el.select('.extras,[class*="extra"]'):
        inn['extras'] = el.get_text(strip=True)
        break
    for el in inn_el.select('.fow,.fall-of-wickets,[class*="wicket"]'):
        txt = el.get_text(strip=True)
        if re.search(r'\d+-\d+', txt):
            inn['fall_of_wickets'] = txt
            break

    if inn['batting_team'] or inn['score'] or inn['batsmen']:
        return inn
    return None

def parse_by_tables(soup):
    innings = []
    for tbl in soup.select('table'):
        hdrs = [th.get_text(strip=True).lower() for th in tbl.select('th')]
        if len(hdrs) >= 3 and any(h in hdrs for h in ['runs','r','batsman','batter']):
            inn = {'batting_team':'','score':'','overs':'','declared':False,
                   'batsmen':[],'bowlers':[],'fall_of_wickets':'','extras':''}
            prev = tbl.find_previous(['h2','h3','h4'])
            if prev:
                inn['batting_team'] = prev.get_text(strip=True)
            for row in tbl.select('tr'):
                cells = [td.get_text(strip=True) for td in row.select('td')]
                if len(cells) >= 3 and cells[0] and cells[0].lower() not in ['batsman','batter','name','player']:
                    inn['batsmen'].append({
                        'name':cells[0],'status':cells[1] if len(cells)>1 else '',
                        'runs':safe_int(cells[2]) if len(cells)>2 else None,
                        'balls':safe_int(cells[3]) if len(cells)>3 else None,
                        'fours':safe_int(cells[4]) if len(cells)>4 else None,
                        'sixes':safe_int(cells[5]) if len(cells)>5 else None,
                        'sr':cells[6] if len(cells)>6 else '',
                    })
            if inn['batsmen']:
                innings.append(inn)
    return innings

def get_cached_or_fetch(url):
    now = time.time()
    if url in cache:
        entry = cache[url]
        if now - entry['timestamp'] < CACHE_SECONDS:
            return entry['data']
    print(f"[proxy] Fetching: {url}")
    try:
        html = fetch_page(url)
        data = parse_scorecard(html, url)
    except Exception as e:
        data = {'error': str(e), 'url': url, 'innings': [], 'match_title': '', 'status': 'Error', 'raw_snippet': ''}
    cache[url] = {'data': data, 'timestamp': now}
    return data

@app.route('/score')
def score():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if 'play-cricket' not in url:
        return jsonify({'error': 'Only Play-Cricket URLs are supported'}), 400
    if 'resultsvault' in url:
        return jsonify({'error': 'ResultsVault URLs cannot be fetched. Use the Play-Cricket scorecard URL instead.'}), 400
    data = get_cached_or_fetch(url)
    return jsonify(data)

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/')
def index():
    return '<html><body style="font-family:sans-serif;padding:30px;background:#0a1a0f;color:#f0ead8"><h2>&#x1F3CF; Cricket Proxy</h2><p style="color:#7de0a0">Running OK</p></body></html>'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
