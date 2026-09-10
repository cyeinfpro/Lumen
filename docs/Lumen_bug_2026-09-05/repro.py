"""Isolated standard-library, selector and token-math checks; not FastAPI/browser tests."""
import hmac, json, sys
from pathlib import Path
from bs4 import BeautifulSoup

def contrast(a,b):
    def luminance(h):
        channels=[int(h[i:i+2],16)/255 for i in (1,3,5)]
        c=[x/12.92 if x<=0.04045 else ((x+.055)/1.055)**2.4 for x in channels]
        return sum(x*y for x,y in zip(c,(.2126,.7152,.0722)))
    a,b=sorted((luminance(a),luminance(b)))
    return (b+.05)/(a+.05)

checks=[]
try:
    hmac.compare_digest('é','0'*64)
    raise AssertionError('Expected TypeError')
except TypeError:
    checks.append({'name':'Non-ASCII string in compare_digest raises TypeError','status':'passed'})

def safe_compare(a,b):
    return bool(a and b and a.isascii() and b.isascii() and hmac.compare_digest(a,b))
assert safe_compare('é','0'*64) is False
assert safe_compare('a'*64,'a'*64) is True
assert safe_compare('a'*64,'b'*64) is False
checks.append({'name':'ASCII validation makes malformed input reject cleanly','status':'passed'})

selector=','.join(['a[href]','button:not([disabled])','textarea:not([disabled])',
 'input:not([disabled]):not([type="hidden"])','select:not([disabled])','summary',
 '[contenteditable="true"]','[tabindex]:not([tabindex="-1"])'])
soup=BeautifulSoup('<input id="query"><button id="close">Close</button><button id="option" tabindex="-1">Result</button><button id="disabled" disabled tabindex="0">No</button>','html.parser')
ids=[el['id'] for el in soup.select(selector)]
assert 'option' in ids and 'disabled' in ids
checks.append({'name':'Current modal selector includes tabindex=-1 and disabled tabindex=0','status':'passed','matches':ids,'scope':'CSS selector matching, not browser focus behavior'})
ratios={
 'current_info_on_white':contrast('#3E9EFF','#FFFFFF'),
 'current_info_on_canvas_light':contrast('#3E9EFF','#F4F5F7'),
 'proposed_info_fg_on_white':contrast('#0D74CE','#FFFFFF'),
 'proposed_info_fg_on_canvas_light':contrast('#0D74CE','#F4F5F7'),
 'command_group_fg3_on_dark_panel':contrast('#5E5951','#121318'),
 'command_group_fg3_on_light_panel':contrast('#A7ADB7','#EAECF0'),
}
assert ratios['current_info_on_white']<4.5
assert ratios['proposed_info_fg_on_white']>=4.5
checks.append({'name':'Color-token contrast calculation','status':'passed','ratios':ratios,'scope':'opaque token combinations, not computed browser styles'})
Path(__file__).resolve().with_name('python-results.json').write_text(json.dumps({'python':sys.version.split()[0],'tests':checks},ensure_ascii=False,indent=2))
print(json.dumps(checks,ensure_ascii=False,indent=2))
