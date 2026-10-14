"""End-to-end UI checks for tournament actions and placement rank with real server API."""
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright
from test_browser import init_browser, signup, ROOT


def test_tournament_browser():
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox'])
        web,client,errors=init_browser(b,1365,900)
        signup(web,'v06-organizer@example.org','TournamentOwner')
        assert '0 / 10' in web.locator('.glow-card').inner_text()
        web.locator('.side-nav [data-go=tournaments]').click()
        web.wait_for_selector('[data-action=new-tournament]')
        web.locator('[data-action=new-tournament]').click()
        f=web.locator('#tournamentForm')
        f.locator('[name=title]').fill('NEXUS WORLD CUP')
        f.locator('[name=slots]').select_option('16')
        f.locator('[name=prize_kzt]').fill('10000')
        future=(datetime.now(timezone.utc)+timedelta(days=14)).astimezone(timezone(timedelta(hours=5)))
        f.locator('[name=starts_at]').fill(future.strftime('%Y-%m-%dT%H:%M'))
        f.locator('[name=registration_closes_at]').fill((future-timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
        f.locator('[name=rules]').fill('Турнир без взносов. 5v5 custom в MLBB.')
        f.locator('[type=submit]').click()
        web.wait_for_selector('.tournament-card:has-text("NEXUS WORLD CUP")')
        assert web.locator('[data-action=edit-tournament]').count()==1
        web.screenshot(path=str(ROOT/'docs'/'tournament_pc_v06.png'),full_page=True)
        web.locator('[data-action=edit-tournament]').click()
        edit=web.locator('#editTournamentForm')
        edit.locator('[name=title]').fill('NEXUS WORLD CUP UPDATED')
        edit.locator('[type=submit]').click()
        web.wait_for_selector('.tournament-card:has-text("NEXUS WORLD CUP UPDATED")')
        web.once('dialog',lambda d:d.accept())
        web.locator('[data-action=toggle-tournament-registration]').click()
        web.wait_for_function("document.querySelector('.tournament-card').innerText.includes('НАБОР ЗАКРЫТ')")
        web.screenshot(path=str(ROOT/'docs'/'tournament_closed_v06.png'),full_page=True)
        mobile,c2,mobile_errors=init_browser(b,390,844)
        signup(mobile,'v06-mobile@example.org','MobileV06')
        assert '0 / 10' in mobile.locator('.glow-card').inner_text()
        mobile.screenshot(path=str(ROOT/'docs'/'calibration_mobile_v06.png'),full_page=True)
        mobile.locator('.mobile-nav [data-go=tournaments]').click()
        mobile.wait_for_selector('.tournament-card:has-text("NEXUS WORLD CUP UPDATED")')
        assert mobile.locator('[data-action=edit-tournament]').count()==0
        assert mobile.locator('.tournament-card').inner_text().find('НАБОР ЗАКРЫТ')>=0
        assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1')
        mobile.screenshot(path=str(ROOT/'docs'/'tournament_mobile_v06.png'),full_page=True)
        assert not mobile_errors, mobile_errors
        mobile.close();c2.close()
        assert not errors, errors
        web.close();client.close();b.close()


if __name__=='__main__':
    test_tournament_browser()
    print('PASS: full tournament create, edit, rendering; calibration screen; JS errors none')
