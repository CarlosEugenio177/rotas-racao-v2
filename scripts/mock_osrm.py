"""OSRM FALSO, só para testar o pipeline sem internet. Responde /table e /route no formato da API real.
Distância = linha reta x 1.3; velocidade cai em pontos 'rurais' (pseudoaleatório). NÃO é dado real."""
import json, math, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs

def hav(a, b):
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def vel(a, b):  # m/s: trechos curtos e "rurais" mais lentos
    d = hav(a, b)
    rural = (int(abs(a[0] * 1000)) + int(abs(b[1] * 1000))) % 3 == 0
    base = 80 if d > 60000 else 60 if d > 15000 else 40
    return (base * (0.6 if rural else 1.0)) / 3.6

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urlsplit(self.path)
        partes = u.path.split("/")
        coords = [tuple(map(float, c.split(","))) for c in partes[-1].split(";")]
        q = parse_qs(u.query)
        if partes[1] == "table":
            src = [int(x) for x in q.get("sources", [";".join(map(str, range(len(coords))))])[0].split(";")]
            dst = [int(x) for x in q.get("destinations", [";".join(map(str, range(len(coords))))])[0].split(";")]
            dist = [[hav(coords[i], coords[j]) * 1.3 for j in dst] for i in src]
            dur = [[dist[a][b] / vel(coords[i], coords[j]) if i != j else 0 for b, j in enumerate(dst)] for a, i in enumerate(src)]
            wp = lambda i: {"location": list(coords[i]), "distance": (abs(coords[i][0] * 7919) % 1) * 1500, "name": "PI-mock"}
            r = {"code": "Ok", "distances": dist, "durations": dur, "sources": [wp(i) for i in src], "destinations": [wp(j) for j in dst]}
        else:
            geo, d, t = [], 0, 0
            for a, b in zip(coords, coords[1:]):
                for k in range(8):
                    geo.append([a[0] + (b[0] - a[0]) * k / 8 + 0.02 * math.sin(k), a[1] + (b[1] - a[1]) * k / 8])
                d += hav(a, b) * 1.3; t += hav(a, b) * 1.3 / vel(a, b)
            geo.append(list(coords[-1]))
            r = {"code": "Ok", "routes": [{"distance": d, "duration": t, "geometry": {"type": "LineString", "coordinates": geo}}]}
        body = json.dumps(r).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 5999), H).serve_forever()
