import os, tempfile,sys,time,subprocess
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
d=tempfile.TemporaryDirectory()
env=os.environ.copy();env.update(NEXUS_DB=str(Path(d.name)/'ui09.sqlite3'),ADMIN_EMAIL='owner-ui09@example.test',ADMIN_PASSWORD='TestOnlyUI09Password!')
p=subprocess.Popen([sys.executable,'-m','uvicorn','server.app:app','--host','127.0.0.1','--port','8769'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
 for i in range(50):
  try:
   if urlopen('http://127.0.0.1:8769/api/status',timeout=.5).status==200:break
  except Exception:time.sleep(.2)
 else:raise RuntimeError(p.stderr.read().decode())
 with sync_playwright() as play:
  browser=play.chromium.launch(headless=True,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1280,'height':800})
  page.goto('http://127.0.0.1:8769/',wait_until='domcontentloaded')
  page.wait_for_timeout(500)
  page.screenshot(path=str(root/'docs'/'login_09.png'))
  page.locator('input[name=email]').fill('owner-ui09@example.test')
  page.locator('input[name=password]').fill('TestOnlyUI09Password!')
  page.locator('form button[type=submit]').first.click()
  page.wait_for_timeout(500)
  print('AFTER_LOGIN',page.locator('main').inner_text()[:110].replace('\n','|'))
  if page.locator('input[name=nickname]').count():
   page.locator('input[name=nickname]').fill('OWNERNEXUS09')
   page.locator('input[name=mlbb_id]').fill('123456789')
   page.locator('input[name=server_id]').fill('1234')
   page.locator('form button[type=submit]').first.click()
   page.wait_for_timeout(600)
  page.locator('[data-go=profile]').first.click()
  page.wait_for_timeout(400)
  assert 'Владелец' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs'/'roles_profile_09.png'))
  page.locator('[data-go=fund]').first.click()
  page.wait_for_timeout(450)
  assert 'Спонсирование пока не подключено' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs'/'sponsor_09.png'))
  page.locator('[data-go=admin]').first.click()
  page.wait_for_timeout(650)
  assert 'Настройка спонсирования' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs'/'admin_09.png'))
  mobile=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  mobile.goto('http://127.0.0.1:8769/',wait_until='domcontentloaded')
  mobile.wait_for_timeout(300)
  mobile.screenshot(path=str(root/'docs'/'mobile_login_09.png'))
  assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1')
  browser.close()
 print('BROWSER_UI_OK')
finally:
 p.terminate()
 try:p.wait(timeout=5)
 except subprocess.TimeoutExpired:p.kill()
 d.cleanup()
