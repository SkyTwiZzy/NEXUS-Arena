"""Live FastAPI TestClient bridged into a network-isolated Playwright Chromium page."""
import os
import sys
import tempfile
import base64
from pathlib import Path

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
os.environ['NEXUS_DB']=str(Path(tempfile.mkdtemp())/'browser.sqlite3')
os.environ['ADMIN_EMAIL']='owner@nexus.local'
os.environ['ADMIN_PASSWORD']='OnlyForBrowserTests123'
sys.path.insert(0,str(ROOT/'server'))
from app import app

logo_data='data:image/svg+xml;base64,'+base64.b64encode((ROOT/'web/icons/nexus-mark.svg').read_bytes()).decode()
HTML=(ROOT/'web'/'index.html').read_text().replace('/icons/nexus-mark.svg',logo_data)
CSS=(ROOT/'web'/'styles.css').read_text()
JS=(ROOT/'web'/'app.js').read_text()


def init_browser(browser,width,height):
    client=TestClient(app)
    page=browser.new_page(viewport={'width':width,'height':height},device_scale_factor=1)
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def bridge(path,verb,raw):
        try:
            response=client.request(verb,path,json=__import__('json').loads(raw) if raw else None)
            return {'status':response.status_code,'body':response.json()}
        except Exception as exc:
            return {'status':500,'body':{'detail':str(exc)}}
    page.expose_function('apiBridge',bridge)
    page.set_content(HTML)
    page.add_style_tag(content=CSS)
    page.add_script_tag(content='''window.fetch=async (path,opts={})=>{const v=await window.apiBridge(path,opts.method||'GET',opts.body||''); return new Response(JSON.stringify(v.body),{status:v.status,headers:{'Content-Type':'application/json'}})};''')
    page.add_script_tag(content=JS)
    page.wait_for_selector('#authForm')
    return page,client,errors


def signup(page,email,nick):
    page.locator('[data-auth=register]').click()
    page.locator('#authForm [name=email]').fill(email)
    page.locator('#authForm [name=password]').fill('SecureBrowserPassword123')
    page.locator('#authForm [name=confirm]').fill('SecureBrowserPassword123')
    page.locator('#authForm [name=accept_terms]').check()
    page.locator('#authForm [type=submit]').click()
    page.wait_for_selector('#profileForm',timeout=12000)
    page.locator('#profileForm [name=nickname]').fill(nick)
    page.locator('#profileForm [name=mlbb_id]').fill('12345678')
    page.locator('#profileForm [name=server_id]').fill('1234')
    page.locator('#profileForm [type=submit]').click()
    
    try:page.wait_for_selector('.hero',timeout=12000)
    except Exception:
      print('AFTER PROFILE', page.locator('main').inner_text()[:1300], 'TOAST',page.locator('#toast').inner_text(), 'PAGEERR',page.locator('body').get_attribute('class'))
      raise


def main():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        desktop,c1,errors1=init_browser(b,1440,900)
        signup(desktop,'web1@nexus.local','NexusAlpha')
        desktop.screenshot(path=str(ROOT/'docs'/'desktop_home.png'),full_page=True)
        desktop.locator('.side-nav [data-go=clans]').click()
        desktop.wait_for_selector('[data-action=create-clan]')
        desktop.locator('[data-action=create-clan]').click()
        desktop.locator('#createClan [name=name]').fill('MoonRiders')
        desktop.locator('#createClan [name=tag]').fill('MNR')
        desktop.locator('#createClan [type=submit]').click()
        desktop.wait_for_selector('#chatForm',timeout=12000)
        desktop.screenshot(path=str(ROOT/'docs'/'desktop_clan.png'),full_page=True)
        mobile,c2,errors2=init_browser(b,390,844)
        signup(mobile,'web2@nexus.local','NexusBeta')
        mobile.locator('.mobile-nav [data-go=clans]').click()
        mobile.wait_for_selector('[data-action=apply-clan]',timeout=15000)
        mobile.locator('[data-action=apply-clan]').first.click()
        desktop.locator('.side-nav [data-go=arena]').click()
        desktop.locator('.side-nav [data-go=clans]').click()
        desktop.wait_for_selector('[data-action=clan-decision][data-decision=approve]',timeout=15000)
        desktop.locator('[data-action=clan-decision][data-decision=approve]').click()
        mobile.locator('.mobile-nav [data-go=arena]').click()
        mobile.locator('.mobile-nav [data-go=clans]').click()
        mobile.wait_for_selector('#chatForm',timeout=15000)
        mobile.locator('#chatForm [name=body]').fill('Привет из мобильной версии!')
        mobile.locator('#chatForm [type=submit]').click()
        desktop.wait_for_timeout(6000)
        desktop.wait_for_selector('.chat-bubble:has-text("Привет из мобильной версии!")',timeout=10000)
        mobile.screenshot(path=str(ROOT/'docs'/'mobile_clan_chat.png'),full_page=True)
        desktop.locator('[data-action=switch-view]').click()
        assert desktop.locator('body').evaluate('(x)=>x.classList.contains("force-mobile")')
        desktop.screenshot(path=str(ROOT/'docs'/'desktop_mobile_preview.png'),full_page=True)
        assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1')
        print('PASS: two real server-backed accounts; clan leader approval; cross-client live chat; desktop+mobile+preview')
        print('JS errors',errors1,errors2)
        assert not errors1 and not errors2
        c1.close();c2.close();b.close()


if __name__=='__main__':main()
