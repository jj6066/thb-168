import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import map2svg as m


def fixture():
    p = m.settings({})
    from pyproj import Transformer, CRS
    inverse=Transformer.from_crs(CRS.from_proj4(f'+proj=aeqd +lat_0={p["lat"]} +lon_0={p["lon"]} +datum=WGS84 +units=m'),4326,always_xy=True).transform
    ways=[]
    for ident,nodes,coords,name in [(1,[1,2,3],[(-140,0),(0,0),(140,0)],'學府路'),(2,[4,2,5],[(-65,-140),(0,0),(65,140)],'科大北路')]:
        geometry=[]
        for x,y in coords:
            lon,lat=inverse(x,y);geometry.append(dict(lon=lon,lat=lat))
        ways.append(dict(type='way',id=ident,nodes=nodes,geometry=geometry,tags={'highway':'residential','name':name,'lanes':'4','width':'13'}))
    return ways

class Tests(unittest.TestCase):
    def test_geometry_and_facilities(self):
        cfg=m.settings({'facilities':True,'islands':True})
        network=m.build_network(fixture(),cfg)
        center=network['junctions']['2']['polygon']
        self.assertTrue(center.contains(m.Point(0,0)))
        self.assertGreater(center.area,100)
        self.assertEqual(len(network['roads']),8)
        self.assertTrue(all(len(r['lanes'])==2 for r in network['roads']))
        self.assertTrue({'s','l','r'} <= {c['direction'] for c in network['connections']})
        svg=m.render_svg(network,cfg)
        ET.fromstring(svg)
        self.assertIn('<circle',svg)
        self.assertIn('sumo-connections',svg)
        self.assertIn('學府路',svg)
        plain,_=m.generate({},fixture())
        self.assertNotIn('<circle',plain)

    def test_width_override_and_escaping(self):
        svg,roads=m.generate({'title':'<script>&','overrides':{'1':{'width':18,'lanes':6}}},fixture())
        ET.fromstring(svg)
        self.assertEqual(roads[0]['width'],18)
        self.assertEqual(roads[0]['lanes'],6)
        self.assertIn('&lt;script&gt;&amp;',svg)

    def test_bridge_not_junction(self):
        ways=fixture();ways[1]['tags']['bridge']='yes'
        network=m.build_network(ways,m.settings({}))
        self.assertTrue(all(r['osm_id']=='1' for r in network['roads']))
        self.assertFalse(any(c['direction'] in ('l','r') for c in network['connections']))

    def test_directional_lanes_and_widths(self):
        ways=fixture()
        ways[0]['tags'].update({'lanes':'3','lanes:forward':'2','lanes:backward':'1','width':'10.5'})
        network=m.build_network(ways,m.settings({}))
        forward=[r for r in network['roads'] if r['osm_id']=='1' and not r['id'].startswith('-')]
        backward=[r for r in network['roads'] if r['osm_id']=='1' and r['id'].startswith('-')]
        self.assertTrue(all(len(r['lanes'])==2 for r in forward))
        self.assertTrue(all(len(r['lanes'])==1 for r in backward))
        self.assertTrue(all(abs(l['width']-3.5)<.01 for r in forward+backward for l in r['lanes']))

    def test_oneway_reverse(self):
        ways=fixture();ways[0]['tags']['oneway']='-1'
        network=m.build_network(ways,m.settings({}))
        roads=[r for r in network['roads'] if r['osm_id']=='1']
        self.assertTrue(roads)
        self.assertTrue(all(r['id'].startswith('-') for r in roads))
        self.assertTrue(all(len(r['lanes'])==4 for r in roads))

    def test_unavailable_per_lane_width_is_labeled(self):
        ways=fixture();ways[0]['tags'].pop('width')
        ways[0]['tags']['width:lanes']='3|3.5|4|4.5'
        result=m.generate_result({},ways)
        row=next(r for r in result['roads'] if r['id']=='1')
        self.assertIn('未採用',row['source'])
        self.assertEqual(row['width'],13)

    def test_missing_binary_is_explicit(self):
        with patch.object(m,'netconvert_binary',side_effect=RuntimeError('missing SUMO')):
            with self.assertRaisesRegex(RuntimeError,'missing SUMO'):
                m.generate({},fixture())

    def test_routes_and_errors(self):
        c=m.app.test_client()
        self.assertEqual(c.get('/').status_code,200)
        for body in [None,[],{'lat':'NaN'},{'default_lanes':2.5},{'bounds':{}},{'radius_m':500},{'facilities':'yes'}]:
            self.assertEqual(c.post('/api/intersection/generate',json=body).status_code,400,body)
        with patch.object(m,'fetch_osm',return_value=fixture()):
            res=c.post('/api/intersection/generate',json={})
            self.assertEqual(res.status_code,200);ET.fromstring(res.data)
            res=c.post('/api/intersection/generate?format=json',json={})
            self.assertEqual(len(res.json['roads']),2)
            self.assertEqual(res.json['engine'],'SUMO netconvert')
            ET.fromstring(res.json['net_xml'])
        with patch.object(m,'fetch_osm',return_value=[]):
            self.assertEqual(c.post('/api/intersection/generate',json={}).status_code,404)
        with patch.object(m,'fetch_osm',side_effect=RuntimeError('temporary')):
            self.assertEqual(c.post('/api/intersection/generate',json={}).status_code,502)

if __name__=='__main__':unittest.main()
