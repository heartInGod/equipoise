import sys, json
for l in sys.stdin:
    try: r = json.loads(l)
    except Exception: print(l.rstrip()); continue
    print(r.get('frame'), r['screen'], r['hero'], '|', r['board'], '| new=', r.get('dec'), '| act=', r['act'], r.get('taps'), '| old=', r.get('old'), r.get('note', ''))
