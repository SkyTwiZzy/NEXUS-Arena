"""Visual browser smoke test without external network requirements."""
import os,tempfile,sys,base64,json
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image
import io
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
TMP=tempfile.TemporaryDirectory()
os.environ['NEXUS_DB']=str(Path(TMP.name)/'browser.sqlite3')
os.environ['ADMIN_EMAIL']='admin08@example.test'
os.environ['ADMIN_PASSWORD']='Admin08PasswordVeryLong!'
sys.path.insert(0,str(ROOT/'server'))
from app import app
HTML=(ROOT/'web/index.html').read_text().replace('/icons/nexus-mark.svg','data:image/svg+xml;base64,'+base64.b64encode((ROOT/'web/icons/nexus-mark.svg').read_bytes()).decode())
CSS=(ROOT/'web/styles.css').read_text()
JS=(ROOT/'web/app.js').read_text()

def browser_page(browser,w,h):
    client=TestClient(app)
    page=browser.new_page(viewport={'width':w,'height':h},device_scale_factor=1)
    errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
    def bridge(path,verb,raw):
        try:
            response=client.request(verb,path,json=json.loads(raw) if raw else None)
            return {'status':response.status_code,'body':response.json()}
        except Exception as exc:return {'status':500,'body':{'detail':str(exc)}}
    page.expose_function('apiBridge',bridge)
    page.set_content(HTML)
    page.add_style_tag(content=CSS)
    page.add_script_tag(content="""window.fetch=async(path,opts={})=>{const v=await apiBridge(path,opts.method||'GET',opts.body||'');return new Response(JSON.stringify(v.body),{status:v.status,headers:{'Content-Type':'application/json'}})};""")
    page.add_script_tag(content=JS)
    page.wait_for_selector('#authForm',timeout=15000)
    return page,client,errors

def player(i):
    c=TestClient(app)
    r=c.post('/api/auth/register',json={'email':f'ui08-{i}@example.test','password':'ExamplePassword12345','confirm':'ExamplePassword12345','accept_terms':True})
    assert r.is_success,r.text
    assert c.patch('/api/me',json={'nickname':f'UI08Player{i}','mlbb_id':f'12345678{i}','server_id':'1234','game_role':'Mid'}).is_success
    assert c.post('/api/lobby',json={}).is_success
    assert c.post('/api/lobby/search',json={}).is_success
    return c

def main():
    clients=[player(i) for i in range(10)]
    m=clients[0].get('/api/matches').json()[0]
    for c in clients:assert c.post(f'/api/matches/{m["id"]}/ready',json={'ready':True}).is_success
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        desktop,dev,errors=browser_page(b,1360,900)
        desktop.locator('#authForm [name=email]').fill('admin08@example.test')
        desktop.locator('#authForm [name=password]').fill('Admin08PasswordVeryLong!')
        desktop.locator('#authForm [type=submit]').click()
        desktop.wait_for_selector('.staff-nav:not([hidden])',timeout=10000)
        desktop.locator('.side-nav [data-go=review]').click()
        desktop.wait_for_selector('.kdaForm',timeout=10000)
        assert desktop.locator('.kdaForm').count()==10
        assert 'Ожидании скриншотов' in desktop.locator('#main').inner_text() or 'В ожидании скриншотов' in desktop.locator('#main').inner_text()
        f=desktop.locator('.kdaForm').first
        f.locator('input[name=kills]').fill('11');f.locator('input[name=deaths]').fill('3');f.locator('input[name=assists]').fill('7')
        f.locator('button').click()
        desktop.wait_for_timeout(800)
        desktop.screenshot(path=str(ROOT/'docs/review_pc_v08.png'),full_page=True)
        mobile,player_session,errs2=browser_page(b,390,844)
        mobile.locator('#authForm [name=email]').fill('ui08-0@example.test')
        mobile.locator('#authForm [name=password]').fill('ExamplePassword12345')
        mobile.locator('#authForm [type=submit]').click()
        mobile.wait_for_selector('.hero',timeout=10000)
        mobile.evaluate("go('matches')")
        mobile.wait_for_selector('.host-card',timeout=10000)
        if mobile.locator('[data-action="submit-evidence"]').count():
            print('UI uploading',flush=True);mobile.locator('[data-action="submit-evidence"]').click()
            mobile.wait_for_selector('#evidenceForm')
            image=Image.new('RGB',(900,500),'#65359b');bio=io.BytesIO();image.save(bio,format='PNG')
            mobile.locator('#evidenceForm [name=screenshot]').set_input_files({'name':'final_score.png','mimeType':'image/png','buffer':bio.getvalue()})
            mobile.locator('#evidenceForm [name=winning_side]').select_option('0')
            print('UI submitting',flush=True);mobile.locator('#evidenceForm [type=submit]').click()
            print('UI waiting close',flush=True);mobile.wait_for_function("document.querySelector('#modal')?.open===false",timeout=6000)
            print('UI waiting state',flush=True);mobile.wait_for_selector('text=Доказательства: 1/2 скриншота',timeout=6000)
        assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth+2')
        mobile.screenshot(path=str(ROOT/'docs/matches_mobile_v08.png'),full_page=True)
        print('Errors',errors,errs2)
        assert not errors and not errs2
        b.close();dev.close();player_session.close()
    for c in clients:c.close()
    print('UI08 PASS')
if __name__=='__main__': main()
