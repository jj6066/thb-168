"""Google Maps selection → OSM geometry → traffic schematic SVG.
Run: .venv/bin/python map2svg.py   (http://127.0.0.1:7999)
Google key: GOOGLE_MAPS_API_KEY environment variable or .env/thi.json.
Requires SUMO netconvert on PATH (or SUMO_HOME). No OSMnx/Uvicorn required.
Facilities are optional schematic proposals, never claimed as surveyed data.
"""
import argparse
import importlib.util
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import lru_cache
import html
import json
import math
import os
from pathlib import Path
import re
import time

from flask import Flask, Response, jsonify, render_template_string, request
from pyproj import CRS, Transformer
import requests
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 128 * 1024


def google_key():
    if os.environ.get('GOOGLE_MAPS_API_KEY'):
        return os.environ['GOOGLE_MAPS_API_KEY']
    try:
        return json.loads((BASE / '.env/thi.json').read_text()).get('GOOGLE_MAPS_API_KEY', '')
    except (OSError, ValueError, AttributeError):
        return ''


def numeric(value, name, low, high):
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{name} 必須是數字') from None
    if not math.isfinite(n) or not low <= n <= high:
        raise ValueError(f'{name} 須介於 {low} 與 {high}')
    return n


def settings(data):
    if not isinstance(data, dict):
        raise ValueError('請傳入 JSON 物件')
    lat = numeric(data.get('lat', 22.64080), '緯度', -80, 80)
    lon = numeric(data.get('lon', 120.59313), '經度', -179.9, 179.9)
    radius = numeric(data.get('radius_m', 85), '半徑（公尺）', 25, 250)
    crs = CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m')
    project = Transformer.from_crs(4326, crs, always_xy=True).transform
    inverse = Transformer.from_crs(crs, 4326, always_xy=True).transform
    b = data.get('bounds')
    if b is not None:
        if not isinstance(b, dict):
            raise ValueError('bounds 必須包含 south, west, north, east')
        s, w, n, e = [numeric(b.get(k), k, -80 if k in ('south', 'north') else -179.9,
                              80 if k in ('south', 'north') else 179.9)
                      for k in ('south', 'west', 'north', 'east')]
        if s >= n or w >= e:
            raise ValueError('框選範圍南北或東西順序錯誤')
        lat, lon = (s+n)/2, (w+e)/2
        crs = CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m')
        project = Transformer.from_crs(4326, crs, always_xy=True).transform
        inverse = Transformer.from_crs(crs, 4326, always_xy=True).transform
        clip = Polygon([project(w,s), project(e,s), project(e,n), project(w,n)])
    else:
        clip = box(-radius, -radius, radius, radius)
    x0, y0, x1, y1 = clip.bounds
    if not (20 <= x1-x0 <= 600 and 20 <= y1-y0 <= 600):
        raise ValueError('框選寬高須各為 20–600 公尺')
    # Fetch margin retains roads whose carriageway overlaps the viewport.
    w,s = inverse(x0-70,y0-70)
    e,n = inverse(x1+70,y1+70)
    lanes = numeric(data.get('default_lanes', 2), '預設車道數', 1, 10)
    if not lanes.is_integer():
        raise ValueError('車道數必須是整數')
    overrides = data.get('overrides', {})
    if not isinstance(overrides, dict):
        raise ValueError('道路設定必須為 JSON 物件')
    for ident, v in overrides.items():
        if not isinstance(v, dict):
            raise ValueError(f'道路 {ident} 設定必須為物件')
        if 'width' in v:
            numeric(v['width'], '道路寬度', 2, 60)
        if 'lanes' in v and not numeric(v['lanes'], '車道數', 1, 10).is_integer():
            raise ValueError('車道數必須是整數')
    for key in ('facilities', 'islands', 'connections'):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f'{key} 必須是布林值')
    return dict(lat=lat, lon=lon, clip=clip, project=project,
                bbox=(s,w,n,e), lanes=int(lanes), overrides=overrides,
                lane_width=numeric(data.get('lane_width_m', 3.25), '車道寬度', 2.5, 4.5),
                corner=numeric(data.get('corner_radius_m', 5), '轉角半徑', 0, 12),
                facilities=data.get('facilities', False), islands=data.get('islands', False),
                connections=data.get('connections', True),
                title=str(data.get('title', '路口交通工程示意圖'))[:80])


@lru_cache(maxsize=32)
def fetch_osm(bbox, time_bucket):
    """Bounded 15 minute in-memory cache. Never cache failed queries."""
    area = ','.join(f'{v:.7f}' for v in bbox)
    query = ('[out:json][timeout:25];way["highway"~'
             '"^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"]'
             f'({area});out geom;>;out body;')
    for endpoint in ('https://overpass-api.de/api/interpreter',
                     'https://overpass.kumi.systems/api/interpreter'):
        try:
            res = requests.get(endpoint, params={'data': query}, timeout=(5, 15),
                                headers={'User-Agent': 'Map2SVG/1.0 (local schematic tool)'})
            res.raise_for_status()
            data = res.json()
            if data.get('remark') or not isinstance(data.get('elements'), list):
                continue
            return list({(e['type'],e['id']):e for e in data['elements']}.values())
        except (requests.RequestException, ValueError):
            continue
    # Small bbox fallback: the official OSM map API returns complete ways + nodes.
    try:
        south, west, north, east = bbox
        res = requests.get('https://api.openstreetmap.org/api/0.6/map.json',
                           params={'bbox': f'{west},{south},{east},{north}'}, timeout=(5, 20))
        res.raise_for_status()
        elements = res.json()['elements']
        nodes = {n['id']: n for n in elements if n['type'] == 'node'}
        allowed = {'motorway','trunk','primary','secondary','tertiary','unclassified',
                   'residential','living_street','service','motorway_link','trunk_link',
                   'primary_link','secondary_link','tertiary_link'}
        return list(nodes.values()) + [dict(w, geometry=[{'lat':nodes[n]['lat'],'lon':nodes[n]['lon']} for n in w['nodes']])
                for w in elements if w['type'] == 'way' and w.get('tags',{}).get('highway') in allowed
                and all(n in nodes for n in w['nodes'])]
    except (requests.RequestException, ValueError, KeyError):
        raise RuntimeError('OSM 道路服務暫時無法回應，請稍後重試或縮小框選範圍。') from None


def osm_number(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(m|meters|metres|ft|\x27)?\s*', str(value or ''))
    if not match:
        return None
    return float(match[1]) * (0.3048 if match[2] in ('ft', "'") else 1)


def netconvert_binary():
    """Use the system installation first, then SUMO_HOME/pip eclipse-sumo."""
    binary = shutil.which('netconvert')
    if binary:
        return binary
    home = os.environ.get('SUMO_HOME')
    if home and (Path(home) / 'bin/netconvert').is_file():
        return str(Path(home) / 'bin/netconvert')
    spec = importlib.util.find_spec('sumo')
    if spec and spec.origin:
        candidate = Path(spec.origin).parent / 'bin/netconvert'
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError('找不到 SUMO netconvert；請安裝 SUMO 或設定 SUMO_HOME。')


def build_network(elements, cfg):
    """Import real OSM topology; all lane/junction coordinates come from SUMO."""
    root = ET.Element('osm', version='0.6', generator='map2svg')
    node_data = {e['id']: e for e in elements if e.get('type') == 'node'}
    nodes, ways, sources = {}, [], {}
    for way in elements:
        if way.get('type') != 'way' or len(way.get('geometry', [])) < 2:
            continue
        tags = dict(way.get('tags', {}))
        if tags.get('bridge', 'no') != 'no' or tags.get('tunnel', 'no') != 'no' or tags.get('layer', '0') != '0':
            continue
        ids = way.get('nodes', [])
        if len(ids) != len(way['geometry']):
            continue
        ident = str(way['id'])
        custom = cfg['overrides'].get(ident, {})
        lane_source = 'OSM lanes'
        if 'lanes' in custom:
            tags['lanes'] = str(int(custom['lanes']))
            # A changed total invalidates old directional splits / turn-lane arrays.
            for key in list(tags):
                if key.startswith('lanes:') or key.startswith('turn:lanes') or key.startswith('width:lanes'):
                    tags.pop(key)
            lane_source = '使用者車道數'
        elif osm_number(tags.get('lanes')) is None:
            forward, backward = osm_number(tags.get('lanes:forward')), osm_number(tags.get('lanes:backward'))
            if forward is not None and backward is not None:
                tags['lanes'] = str(int(forward + backward))
                lane_source = 'OSM 分向車道數'
            elif forward is None and backward is None:
                tags['lanes'] = str(cfg['lanes'])
                lane_source = '預設車道數（推估）'
            else:
                lane_source = 'OSM 分向資料＋SUMO 補值'
        width_source = '預設車道寬（推估）'
        tagged_width = osm_number(tags.get('width'))
        if 'width' in custom:
            tags['width'] = str(float(custom['width']))
            for key in list(tags):
                if key.startswith('width:lanes') or key.startswith('width:forward') or key.startswith('width:backward'):
                    tags.pop(key)
            width_source = '使用者道路寬'
        elif tagged_width and 2 <= tagged_width <= 60:
            tags['width'] = str(tagged_width)
            width_source = 'OSM width'
        else:
            tags.pop('width', None)
            if any(k.startswith('width:lanes') for k in tags):
                width_source = 'width:lanes 未採用；預設車道寬（推估）'
        sources[ident] = dict(name=tags.get('name', tags.get('ref', '未命名道路')),
                             source=f'{width_source}；{lane_source}',
                             oneway=tags.get('oneway') in ('yes','1','true','-1') or tags.get('junction')=='roundabout')
        for node, point in zip(ids, way['geometry']):
            nodes[node] = point
        ways.append((way, tags))
    if not ways:
        raise LookupError('框選範圍查無地面道路；請調整位置或放大範圍。')
    for node, point in nodes.items():
        element = ET.SubElement(root, 'node', id=str(node), lat=str(point['lat']), lon=str(point['lon']))
        for key, value in node_data.get(node, {}).get('tags', {}).items():
            ET.SubElement(element, 'tag', k=key, v=str(value))
    for way, tags in ways:
        element = ET.SubElement(root, 'way', id=str(way['id']))
        for node in way['nodes']:
            ET.SubElement(element, 'nd', ref=str(node))
        for key, value in tags.items():
            ET.SubElement(element, 'tag', k=key, v=str(value))
    projection = f'+proj=aeqd +lat_0={cfg["lat"]} +lon_0={cfg["lon"]} +datum=WGS84 +units=m'
    with tempfile.TemporaryDirectory(prefix='map2svg-') as directory:
        source, output = Path(directory)/'roads.osm.xml', Path(directory)/'roads.net.xml'
        ET.ElementTree(root).write(source, encoding='utf-8', xml_declaration=True)
        command = [netconvert_binary(), '--osm-files', str(source), '--output-file', str(output),
                   '--proj', projection, '--offset.disable-normalization',
                   '--output.original-names', '--output.street-names', '--osm.turn-lanes',
                   '--junctions.join', '--junctions.join-dist', '15',
                   '--junctions.corner-detail', '12', '--junctions.internal-link-detail', '12',
                   '--default.junctions.radius', str(cfg['corner']),
                   '--default.lanewidth', str(cfg['lane_width']), '--no-turnarounds',
                   '--no-warnings']
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('SUMO netconvert 無法執行或逾時，請縮小框選範圍。') from exc
        if result.returncode or not output.is_file():
            app.logger.warning('netconvert failed: %s', result.stderr[-2000:])
            raise RuntimeError('SUMO 無法建立道路網路，請調整範圍或車道設定。')
        # Remove generated command comments containing temporary local paths.
        net = ET.parse(output).getroot()
    location = net.find('location')
    ox, oy = map(float, location.get('netOffset', '0,0').split(','))
    to_geo = Transformer.from_crs(CRS.from_user_input(location.get('projParameter')), 4326, always_xy=True).transform
    def shape(value):
        coords = []
        for token in value.split():
            x,y = map(float, token.split(',')[:2])
            coords.append(cfg['project'](*to_geo(x-ox,y-oy)))
        return coords
    roads, internal, lane_map = [], [], {}
    for edge in net.findall('edge'):
        lanes = []
        for lane in edge.findall('lane'):
            coords = shape(lane.get('shape', ''))
            if len(coords) < 2:
                continue
            item = dict(id=lane.get('id'), line=LineString(coords),
                        width=float(lane.get('width', cfg['lane_width'])), index=int(lane.get('index')))
            lanes.append(item)
            lane_map[item['id']] = item
        if not lanes:
            continue
        if edge.get('function'):
            internal.extend(lanes)
            continue
        original = edge.find('.//param[@key="origId"]')
        ident = original.get('value').lstrip('-') if original is not None else edge.get('id').lstrip('-').split('#')[0]
        # With geometry merging disabled each SUMO edge retains one OSM way id.
        info = sources.get(ident, dict(name=edge.get('name','未命名道路'),source='SUMO 匯入',oneway=True))
        roads.append(dict(id=edge.get('id'), osm_id=ident, name=info['name'], source=info['source'],
                          oneway=info['oneway'], lanes=lanes, width=sum(l['width'] for l in lanes),
                          start=edge.get('from'), end=edge.get('to')))
    junctions = {}
    for junction in net.findall('junction'):
        coords = shape(junction.get('shape',''))
        if junction.get('type') == 'internal' or len(coords)<3:
            continue
        poly=Polygon(coords).buffer(0)
        junctions[junction.get('id')] = dict(polygon=poly, type=junction.get('type'),
                                            incoming=junction.get('incLanes','').split())
    connections = []
    for connection in net.findall('connection'):
        if connection.get('from','').startswith(':'):
            continue
        lane_id=f'{connection.get("from")}_{connection.get("fromLane")}'
        if lane_id in lane_map:
            connections.append(dict(lane=lane_id, direction=connection.get('dir','s'),
                                    via=connection.get('via'), signal=connection.get('tl')))
    if not roads:
        raise LookupError('SUMO 未產生可用道路。')
    return dict(roads=roads,junctions=junctions,internal=internal,connections=connections,
                net_xml=ET.tostring(net,encoding='unicode'), sources=sources)


def pieces(geom, kind):
    if geom.is_empty:
        return []
    if geom.geom_type == kind:
        return [geom]
    return [part for g in getattr(geom, 'geoms', []) for part in pieces(g, kind)]


def render_svg(network, cfg):
    clip=cfg['clip']
    x0,y0,x1,y1=clip.bounds
    scale=min(1100/(x1-x0),780/(y1-y0))
    width,height=(x1-x0)*scale+60,(y1-y0)*scale+160
    def xy(x,y):return 30+(x-x0)*scale,110+(y1-y)*scale
    def path(line,close=False):
        return 'M '+' L '.join(f'{a:.2f},{b:.2f}' for a,b in [xy(x,y) for x,y in line.coords])+(' Z' if close else '')
    def polygon(geom):
        return ' '.join(path(p.exterior,True)+' '+' '.join(path(r,True) for r in p.interiors) for p in pieces(geom,'Polygon'))
    def stroke(geom,color='#999',size=.15,dash=None):
        return ''.join(f'<path d="{path(g)}" fill="none" stroke="{color}" stroke-width="{size*scale:.2f}"'+
                       (f' stroke-dasharray="{dash[0]*scale:.2f} {dash[1]*scale:.2f}"' if dash else '')+'/>' for g in pieces(geom,'LineString'))
    roads=network['roads']; junctions=network['junctions']
    footprints=[l['line'].buffer(l['width']/2,cap_style=2) for r in roads for l in r['lanes']]
    # SUMO already computes junction outlines; do not invent/smooth junctions here.
    surface=unary_union(footprints+[j['polygon'] for j in junctions.values()])
    # netconvert rounds lane coordinates to centimetres; weld sub-centimetre seams.
    surface=surface.buffer(.02,join_style=2).buffer(-.02,join_style=2)
    visible=surface.intersection(clip)
    if visible.is_empty:
        raise LookupError('框選範圍沒有 SUMO 道路，請放大或移動範圍。')
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" viewBox="0 0 {width:.2f} {height:.2f}" role="img">',
           f'<title>{html.escape(cfg["title"])}</title><desc>SUMO netconvert 車道幾何、路口形狀與轉向連接；缺少的寬度及車道數依設定推估。</desc>',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<defs><clipPath id="viewport"><path d="{polygon(clip)}"/></clipPath><clipPath id="road"><path d="{polygon(visible)}" fill-rule="evenodd"/></clipPath>',
           '<pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(35)"><path d="M 0 0 V 8" stroke="#777" stroke-width="2"/></pattern></defs>',
           '<g font-family="Noto Sans CJK TC,Microsoft JhengHei,sans-serif">',
           f'<text x="30" y="39" font-size="27" font-weight="bold">{html.escape(cfg["title"])}</text>',
           f'<text x="30" y="66" font-size="13" fill="#555">SUMO netconvert · {cfg["lat"]:.6f}, {cfg["lon"]:.6f} · 車道／路口／轉向幾何</text>',
           '<g clip-path="url(#viewport)">',
           f'<path d="{polygon(visible)}" fill="#fcfcfc" fill-rule="evenodd"/>',
           stroke(surface.boundary.intersection(clip),'#222',.25),
           '<g id="sumo-lane-markings" clip-path="url(#road)">']
    edge_lookup={r['id']:r for r in roads}
    for r in roads:
        lanes=sorted(r['lanes'],key=lambda l:l['index'])
        for lane in lanes[:-1]:
            parts.append(stroke(lane['line'].offset_curve(lane['width']/2),dash=(3,4)))
        # Draw a center divider only if the paired opposite SUMO edge is adjacent.
        opposite=edge_lookup.get(r['id'][1:] if r['id'].startswith('-') else '-'+r['id'])
        if opposite and not r['id'].startswith('-'):
            left=lanes[-1]
            divider=left['line'].offset_curve(left['width']/2)
            other=opposite['lanes'][-1]
            other_boundary=other['line'].offset_curve(other['width']/2)
            if divider.distance(other_boundary)<.5:
                for offset in (-.12,.12):
                    parts.append(stroke(divider.offset_curve(offset),'#eab308',.1))
    parts.append('</g><g id="sumo-connections" clip-path="url(#road)">')
    if cfg['connections']:
        for lane in network['internal']:
            parts.append(stroke(lane['line'],'#bbc1c5',.10,(1.2,1.8)))
    parts.append('</g><g id="schematic-facilities" clip-path="url(#road)">')
    directions=defaultdict(set)
    for c in network['connections']:directions[c['lane']].add(c['direction'])
    for r in roads:
        j=junctions.get(r['end'])
        if not j or j['type'] in ('dead_end','internal') or len(j['incoming'])<2:
            continue
        for lane in r['lanes']:
            line=lane['line']
            if line.length<16:continue
            # Lane geometry ends at SUMO's junction boundary, including stop offsets.
            a,b=line.interpolate(line.length-1),line.interpolate(line.length)
            dx,dy=b.x-a.x,b.y-a.y;norm=math.hypot(dx,dy)
            if norm<.001:continue
            ux,uy=dx/norm,dy/norm;nx,ny=-uy,ux
            def local(along,across):return (b.x+ux*along+nx*across,b.y+uy*along+ny*across)
            half=lane['width']/2
            if cfg['facilities']:
                # Optional proposals; never presented as OSM surveyed crossings/signals.
                for i in range(max(1,int(lane['width']/1.1))):
                    lateral=-half+i*1.1+.15
                    stripe=Polygon([local(-4,lateral),local(-.5,lateral),local(-.5,lateral+.5),local(-4,lateral+.5)])
                    parts.append(f'<path d="{polygon(stripe)}" fill="#aaa"/>')
                parts.append(stroke(LineString([local(-5,-half),local(-5,half)]),'#888',.35))
            for direction in directions[lane['id']]:
                # The arrow direction comes from SUMO connection.dir, not a blanket straight arrow.
                sign=1 if direction in ('l','L') else -1
                if direction in ('l','L','r','R'):
                    coords=[local(-13,0),local(-10,0),local(-10,sign*min(1,half-.2))]
                    tip=coords[-1]
                    arrow=Polygon([tip,local(-10-.6,sign*.35),local(-10+.6,sign*.35)])
                elif direction=='s':
                    coords=[local(-13,0),local(-8,0)]
                    arrow=Polygon([coords[-1],local(-9.4,-.6),local(-9.4,.6)])
                else:continue
                parts.append(stroke(LineString(coords),'#999',.23))
                parts.append(f'<path d="{polygon(arrow)}" fill="#999"/>')
        if cfg['facilities']:
            lane=r['lanes'][0];p=lane['line'].interpolate(max(0,lane['line'].length-5))
            px,py=xy(p.x,p.y)
            parts.append(f'<g transform="translate({px:.2f} {py:.2f})"><rect x="-12" y="-5" width="24" height="10" rx="3" fill="#222"/>'+''.join(f'<circle cx="{i*7-7}" cy="0" r="3" fill="{c}"/>' for i,c in enumerate(['#ef4444','#facc15','#4caf50']))+'</g>')
    parts.append('</g><g id="sumo-enclosed-gaps">')
    if cfg['islands']:
        # Only hatch actual enclosed gaps in SUMO's road footprint, never invent a curb island.
        for p in pieces(visible,'Polygon'):
            for ring in p.interiors:
                island=Polygon(ring)
                if island.area>=1:
                    parts.append(f'<path d="{polygon(island)}" fill="url(#hatch)" stroke="#eab308" stroke-width="2"/>')
    parts.append('</g><g id="road-labels">')
    placed=[]
    for r in roads:
        candidates=pieces(r['lanes'][0]['line'].intersection(clip),'LineString')
        if not candidates:continue
        line=max(candidates,key=lambda l:l.length)
        if line.length<20:continue
        p=line.interpolate(.55,normalized=True);px,py=xy(p.x,p.y)
        if any(math.hypot(px-x,py-y)<110 for x,y in placed):continue
        a,b=line.interpolate(.45,normalized=True),line.interpolate(.65,normalized=True)
        angle=math.degrees(math.atan2(-(b.y-a.y),b.x-a.x))
        if angle>90:angle-=180
        if angle<-90:angle+=180
        parts.append(f'<text transform="translate({px:.2f} {py:.2f}) rotate({angle:.2f})" text-anchor="middle" font-size="18" stroke="#fff" stroke-width=".5" fill="#111">{html.escape(r["name"])}</text>')
        placed.append((px,py))
    parts.append('</g></g>')
    bar=20 if x1-x0>100 else 10
    parts.append(f'<g transform="translate({width-40:.2f} 30)"><path d="M 0 40 V 0 l -5 12 M 0 0 l 5 12" fill="none" stroke="#111" stroke-width="2"/><text x="-6" y="-8" font-size="17">N</text></g>')
    parts.append(f'<path d="M 30 88 h {bar*scale:.2f} m 0 -5 v 10 M 30 83 v 10" stroke="#222" fill="none"/><text x="{40+bar*scale:.2f}" y="93" font-size="12">{bar} m</text>')
    note='斑馬線／號誌為示意配置；' if cfg['facilities'] else ''
    parts.append(f'<text x="30" y="{height-25:.2f}" font-size="12" fill="#555">{note}車道與轉向依 OSM／SUMO 推算，非施工測量圖</text>')
    parts.append(f'<a href="https://www.openstreetmap.org/copyright"><text x="30" y="{height-8:.2f}" font-size="11" fill="#555">© OpenStreetMap contributors · ODbL · 地面道路 · SUMO netconvert</text></a></g></svg>')
    return ''.join(parts)


def generate_result(data, elements=None):
    cfg=settings(data)
    if elements is None:
        elements=fetch_osm(tuple(round(v,7) for v in cfg['bbox']),int(time.time()//900))
    network=build_network(elements,cfg)
    # One table row per original OSM way. Splits do not multiply lane counts.
    grouped=defaultdict(dict)
    for road in network['roads']:
        if not any(l['line'].buffer(l['width']/2).intersects(cfg['clip']) for l in road['lanes']):continue
        direction=road['id'].startswith('-')
        previous=grouped[road['osm_id']].get(direction)
        if previous is None or len(road['lanes'])>len(previous['lanes']):
            grouped[road['osm_id']][direction]=road
    summary=[]
    for ident,directions in grouped.items():
        roads=list(directions.values())
        summary.append(dict(id=ident,name=roads[0]['name'],source=roads[0]['source'],
                            oneway=roads[0]['oneway'],width=round(sum(r['width'] for r in roads),2),
                            lanes=sum(len(r['lanes']) for r in roads)))
    return dict(svg=render_svg(network,cfg),roads=summary,net_xml=network['net_xml'],engine='SUMO netconvert',
                lane_count=sum(len(r['lanes']) for r in network['roads']),
                connection_count=len(network['connections']))


def generate(data,elements=None):
    result=generate_result(data,elements)
    return result['svg'],result['roads']


@app.post('/api/intersection/generate')
def generate_intersection():
    try:
        data=request.get_json(silent=True)
        result=generate_result(data)
        svg=result['svg']
        if request.args.get('format')=='json':
            return jsonify(result)
        return Response(svg,mimetype='image/svg+xml',headers={'Content-Disposition':'inline; filename="intersection.svg"'})
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    except LookupError as exc:
        return jsonify(error=str(exc)),404
    except RuntimeError as exc:
        return jsonify(error=str(exc)),502
    except Exception:
        app.logger.exception('Schematic generation failed')
        return jsonify(error='道路幾何處理失敗，請縮小範圍或調整設定。'),500


PAGE = r'''<!doctype html>
<html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>路口交通工程示意圖</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f7;color:#172e3b;font:15px system-ui,sans-serif}header{padding:18px 24px;background:#113e50;color:white}h1{font-size:22px;margin:0 0 6px}header p{margin:0;color:#c8e0e9;font-size:13px}.controls{padding:15px;display:flex;gap:10px;align-items:end;flex-wrap:wrap;background:white;border-bottom:1px solid #dbe3e7}label{display:flex;flex-direction:column;gap:4px;font-size:12px}input{width:110px;padding:8px;border:1px solid #b5c6ce;border-radius:5px}input[type=checkbox]{width:auto}button,.download{padding:10px 14px;border:0;border-radius:5px;background:#087e8b;color:white;cursor:pointer;text-decoration:none}button:disabled{opacity:.5;cursor:wait}.secondary{background:#e4edf0;color:#234}.workspace{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px}.panel{background:white;border:1px solid #dce4e8;border-radius:8px;overflow:hidden}.panel h2{font-size:15px;padding:13px;margin:0;border-bottom:1px solid #e4ebef}#map{height:580px}#preview{height:580px;display:flex;align-items:center;justify-content:center;padding:14px;background:#fafafa}#preview img{width:100%;height:100%;object-fit:contain}#status{padding:12px 20px;min-height:44px}details{padding:12px 20px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:7px;text-align:left;border-bottom:1px solid #ddd}td input{width:80px}.hint{color:#667e87;font-size:13px}.check{display:block;padding:8px}.download[hidden]{display:none}@media(max-width:900px){.workspace{grid-template-columns:1fr}#map,#preview{height:460px}}
</style><header><h1>路口交通工程示意圖</h1><p>Google Maps 選取範圍 → OpenStreetMap 道路中心線 → SUMO 車道／路口／轉向 → SVG</p></header>
<div class="controls">
<label>緯度<input id="lat" type="number" step="any" value="22.64080"></label><label>經度<input id="lon" type="number" step="any" value="120.59313"></label><label>半徑 m<input id="radius" type="number" min="25" max="250" value="85"></label>
<button id="locate">定位／重設框選</button><button id="select" class="secondary">點兩角框選</button>
<label>預設總車道<input id="lanes" type="number" min="1" max="10" value="2"></label><label>車道寬 m<input id="laneWidth" type="number" min="2.5" max="4.5" step=".1" value="3.25"></label><label>轉角半徑 m<input id="corner" type="number" min="0" max="12" value="5"></label>
<label>圖名<input id="title" style="width:210px" value="路口交通工程示意圖"></label>
<label class="check"><input id="facilities" type="checkbox"> 加入示意斑馬線、號誌</label><label class="check"><input id="islands" type="checkbox"> 標示道路封閉空隙</label>
<label class="check"><input id="connections" type="checkbox" checked> 顯示 SUMO 轉向連接</label><button id="generate">產生 SVG</button><a class="download" id="download" download="intersection.svg" hidden>下載 SVG</a><a class="download" id="downloadNet" download="intersection.net.xml" hidden>下載 SUMO／NetEdit</a>
</div><div id="status" role="status" aria-live="polite">輸入經緯度或在地圖選取路口，拖曳框線控制點可調整範圍。</div>
<div class="workspace"><section class="panel"><h2>01 ／ 選取路口 · 框選寬高 20–600 m</h2><div id="map"></div></section><section class="panel"><h2>02 ／ SVG 工程示意圖 · 北向朝上</h2><div id="preview"><p class="hint">選取路口後按「產生 SVG」</p></div></section></div>
<details open><summary>道路寬度與車道校正</summary><p class="hint">產生後可逐條修改寬度與車道數，再按產生 SVG。寬度優先採用 OSM width；缺值以車道數推估。車道形狀、路口及轉向連接由 SUMO 計算；缺值用預設補足，仍需現場校核。道路寬度及車道數是整條 OSM way 的雙向合計；分隔道路每條 way 各自計算。橋梁、隧道及非地面圖層不繪製。</p><table><thead><tr><th>OSM way</th><th>路名</th><th>寬度來源</th><th>寬 m</th><th>車道數</th></tr></thead><tbody id="roads"></tbody></table></details>
<script>
const $=id=>document.getElementById(id);let map,rect,firstCorner,choosing=false,currentUrl,netUrl,overrides={};
function status(t){$('status').textContent=t}
function coordinateBounds(){const lat=Number($('lat').value),lon=Number($('lon').value),r=Number($('radius').value);return {south:lat-r/111320,north:lat+r/111320,west:lon-r/(111320*Math.cos(lat*Math.PI/180)),east:lon+r/(111320*Math.cos(lat*Math.PI/180))}}
function setSelection(b){if(!map)return;if(!rect){rect=new google.maps.Rectangle({map,bounds:b,editable:true,draggable:true,fillColor:'#00a4a6',fillOpacity:.12,strokeColor:'#007f8b',strokeWeight:2});rect.addListener('bounds_changed',()=>{const c=rect.getBounds().getCenter();$('lat').value=c.lat().toFixed(7);$('lon').value=c.lng().toFixed(7)})}else rect.setBounds(b);map.fitBounds(b)}
window.initMap=()=>{map=new google.maps.Map($('map'),{center:{lat:22.64080,lng:120.59313},zoom:18,mapTypeControl:true,streetViewControl:false});setSelection(coordinateBounds());map.addListener('click',e=>{if(choosing){if(!firstCorner){firstCorner=e.latLng;status('已選第一角，請點選對角。')}else{setSelection({south:Math.min(firstCorner.lat(),e.latLng.lat()),north:Math.max(firstCorner.lat(),e.latLng.lat()),west:Math.min(firstCorner.lng(),e.latLng.lng()),east:Math.max(firstCorner.lng(),e.latLng.lng())});firstCorner=null;choosing=false;map.setOptions({draggableCursor:null});status('已框選，可拖曳矩形調整。')}}else{$('lat').value=e.latLng.lat().toFixed(7);$('lon').value=e.latLng.lng().toFixed(7);setSelection(coordinateBounds())}})};
window.gm_authFailure=()=>status('Google Maps 驗證失敗，請確認 Key、Maps JavaScript API 與網址限制；仍可輸入經緯度產圖。');
$('locate').onclick=()=>{setSelection(coordinateBounds());overrides={};$('roads').replaceChildren();status('已定位；可調整框選後產圖。')};
for(const id of ['lat','lon','radius']) $(id).addEventListener('change',()=>setSelection(coordinateBounds()));
$('select').onclick=()=>{if(!map){status('Google 地圖尚未載入，仍可輸入經緯度產圖。');return}choosing=true;firstCorner=null;map.setOptions({draggableCursor:'crosshair'});status('請在地圖點第一角，再點對角。')};
$('generate').onclick=async()=>{const btn=$('generate');btn.disabled=true;$('download').hidden=true;$('downloadNet').hidden=true;status('正在取得 OSM 道路並產生幾何圖形，首次查詢可能需約 1 分鐘…');try{const body={lat:Number($('lat').value),lon:Number($('lon').value),radius_m:Number($('radius').value),default_lanes:Number($('lanes').value),lane_width_m:Number($('laneWidth').value),corner_radius_m:Number($('corner').value),title:$('title').value,facilities:$('facilities').checked,islands:$('islands').checked,connections:$('connections').checked,overrides};if(rect)body.bounds=rect.getBounds().toJSON();const response=await fetch('/api/intersection/generate?format=json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw Error(result.error||'產圖失敗');if(currentUrl)URL.revokeObjectURL(currentUrl);currentUrl=URL.createObjectURL(new Blob([result.svg],{type:'image/svg+xml'}));const img=new Image();img.alt='路口交通工程示意圖';img.src=currentUrl;$('preview').replaceChildren(img);$('download').href=currentUrl;$('download').hidden=false;if(netUrl)URL.revokeObjectURL(netUrl);netUrl=URL.createObjectURL(new Blob([result.net_xml],{type:'application/xml'}));$('downloadNet').href=netUrl;$('downloadNet').hidden=false;$('roads').replaceChildren();for(const road of result.roads){const row=document.createElement('tr');for(const val of [road.id,road.name,road.source]){const td=document.createElement('td');td.textContent=val;row.append(td)}for(const key of ['width','lanes']){const td=document.createElement('td'),input=document.createElement('input');input.type='number';input.min=key==='width'?2:1;input.max=key==='width'?60:10;input.step=key==='width'?.1:1;input.value=road[key];input.onchange=()=>{overrides[road.id]={...(overrides[road.id]||{}),[key]:Number(input.value)}};td.append(input);row.append(td)}$('roads').append(row)}status(`完成：${result.engine} · ${result.roads.length} 條 OSM 道路 · ${result.lane_count} 條車道 · ${result.connection_count} 個轉向連接（含框外緩衝區）。SVG 可下載；交通設施與車道配置需現場校核。`)}catch(e){status(e.message)}finally{btn.disabled=false}};
const key={{ key|tojson }};
if(key){const s=document.createElement('script');s.src='https://maps.googleapis.com/maps/api/js?key='+encodeURIComponent(key)+'&loading=async&callback=initMap&v=weekly';s.async=true;s.onerror=()=>status('Google Maps 載入失敗，仍可輸入經緯度產圖。');document.head.append(s)}else{$('map').textContent='未設定 GOOGLE_MAPS_API_KEY。可先使用經緯度產圖。'}
</script></html>'''


@app.get('/')
def page():
    return render_template_string(PAGE,key=google_key())


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=7999)
    args=parser.parse_args()
    app.run(host=args.host,port=args.port,debug=False)
