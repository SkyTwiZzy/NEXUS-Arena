"""Live responsive smoke test; launches system Chromium explicitly."""
import os, tempfile, sys, subprocess,time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
d=tempfile.TemporaryDirectory()
env=os.environ.copy();env.update(NEXUS_DB=str(Path(d.name)/'ui10.sqlite3'),ADMIN_EMAIL='owner-ui10@example.test',ADMIN_PASSWORD='TestOnlyUI10Password!')
server=subprocess.Popen([sys.executable,'-m','uvicorn','server.app:app','--host','127.0.0.1','--port','8798'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
 for _ in range(75):
  try:
   if urlopen('http://127.0.0.1:8798/api/status',timeout=.5).status==200:break
  except Exception:time.sleep(.2)
 else:raise RuntimeError('Server did not start: '+server.stderr.read().decode())
 with sync_playwright() as play:
  browser=play.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':1320,'height':850})
  errors=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto('http://127.0.0.1:8798/',wait_until='domcontentloaded')
  page.locator('input[name=email]').fill('owner-ui10@example.test')
  page.locator('input[name=password]').fill('TestOnlyUI10Password!')
  page.locator('#authForm button[type=submit]').click()
  page.locator('input[name=nickname]').fill('NEXUS10_OWNER')
  page.locator('input[name=mlbb_id]').fill('123456789')
  page.locator('input[name=server_id]').fill('9999')
  page.locator('form button[type=submit]').first.click()
  page.wait_for_timeout(350)
  assert 'Ранги по режимам' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs/modes_desktop_10.png'),full_page=True)
  page.locator('[data-go="lobby"]').first.click()
  page.locator('[data-action="create-lobby"]').click()
  page.locator('[data-action="change-lobby-mode"][data-mode="1v1"]').click()
  page.wait_for_timeout(200)
  assert 'Дуэль 1×1' in page.locator('main').inner_text()
  assert '1/1 игроков' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs/lobby_1v1_desktop_10.png'),full_page=True)
  page.locator('[data-action="change-lobby-mode"][data-mode="2v2"]').click()
  assert 'Дуэт 2×2' in page.locator('main').inner_text()
  page.locator('[data-go="profile"]').first.click()
  assert 'K/D' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs/profile_ranks_desktop_10.png'),full_page=True)
  page.locator('[data-go="leaderboard"]').first.click()
  page.locator('[data-action="leaderboard-mode"][data-mode="2v2"]').click()
  assert 'Дуэт 2×2' in page.locator('main').inner_text()
  page.screenshot(path=str(root/'docs/leaderboard_2v2_desktop_10.png'),full_page=True)
  # Narrow viewport while still signed in.
  page.set_viewport_size({'width':390,'height':844})
  page.locator('[data-go="lobby"]').first.click()
  page.wait_for_timeout(350)
  assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1'), f"overflow {page.evaluate('document.documentElement.scrollWidth')}"
  page.screenshot(path=str(root/'docs/lobby_mobile_10.png'),full_page=True)
  assert not errors,errors
  browser.close()
 print('UI_SMOKE_TEST_OK')
finally:
 server.terminate()
 try:server.wait(timeout=5)
 except subprocess.TimeoutExpired:server.kill()
 d.cleanup()
