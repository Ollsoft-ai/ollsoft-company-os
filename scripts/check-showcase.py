#!/usr/bin/env python3
"""Browser checks for showcase artifacts; no production services or data needed."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1] / 'showcase/kb'
# The templates name people by placeholder; seed-showcase.sh fills in the real
# --admin and --member. Check them filled with the repo's demo identities.
PEOPLE = {'{{admin}}': 'alice', '{{member}}': 'bob', '{{Admin}}': 'Alice', '{{Member}}': 'Bob'}


def render(path):
    text = (ROOT / path).read_text()
    for placeholder, value in PEOPLE.items():
        text = text.replace(placeholder, value)
    return text


def mount(page, path, state_path=None, rows=None):
    page.goto('about:blank')
    state = {state_path: render(state_path)} if state_path else {}
    page.evaluate('''({state, rows}) => {
      window.files = state; window.uploads = []; window.deny = false;
      addEventListener('message', e => {
        const m=e.data; if (!m?.type?.startsWith('kb-')) return;
        let r;
        if(m.type==='kb-read') r={ok:true,content:files[m.path]};
        if(m.type==='kb-write') {if(window.deny)r={ok:false,error:'Permission denied'};
          else {files[m.path]=m.content;r={ok:true,bytes:m.content.length}}}
        if(m.type==='kb-query')r={rows:m.sql==='SELECT current_user'?[['bob']]:rows};
        if(m.type==='kb-upload'){uploads.push(m);r={ok:true,path:'company/finance/_files/'+m.name}}
        e.source.postMessage({type:'kb-result',id:m.id,...r},'*');
      });
    }''', {'state': state, 'rows': rows or []})
    page.evaluate('html => {const f=document.createElement("iframe");f.sandbox="allow-scripts";f.style="width:100%;height:900px;border:0";f.srcdoc=html;document.body.append(f)}', render(path))
    return page.frame_locator('iframe')


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1050})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        path = 'company/sales/.pipeline-data.json'
        f = mount(page, 'company/sales/customer-pipeline.html', path)
        expect(f.locator('.card')).to_have_count(6)
        f.get_by_role('button', name='Edit review for Elbe Verpackung GmbH').click()
        f.locator('#stage').select_option('proposal')
        f.locator('#save').click()
        expect(f.locator('#review-error')).to_contain_text('Record both reviews')
        f.locator('#requirements').check()
        f.locator('#security').check()
        f.locator('#evidence').fill('Workshop reviewed by Bob; sample evidence.')
        page.evaluate('window.deny=true')
        f.locator('#save').click()
        expect(f.locator('#review-error')).to_contain_text('Permission denied')
        assert json.loads(page.evaluate('(p)=>files[p]', path))['opportunities'][2]['stage'] != 'proposal'
        page.evaluate('window.deny=false')
        f.locator('#save').click()
        expect(f.locator('#review')).not_to_be_visible()
        saved = json.loads(page.evaluate('(p)=>files[p]', path))
        elbe = next(o for o in saved['opportunities'] if o['id'] == 'OP-1038')
        assert elbe['stage'] == 'proposal' and elbe['reviewedBy'] == 'bob'
        f.get_by_role('button', name='Edit review for Elbe Verpackung GmbH').click()
        page.evaluate('(p)=>files[p]+=" "', path)
        f.locator('#save').click()
        expect(f.locator('#review-error')).to_contain_text('Another visitor')
        print('PASS pipeline: review gate, denied save, persistence, stale-edit protection')

        path = 'company/finance/.invoice-data.json'
        f = mount(page, 'company/finance/invoice-generator.html', path)
        expect(f.locator('#status')).to_contain_text('Saved draft loaded')
        f.locator('#qty0').fill('-1')
        expect(f.locator('#save')).to_be_disabled()
        expect(f.locator('#print')).to_be_disabled()
        f.locator('#qty0').fill('2')
        f.locator('#price0').fill('')
        expect(f.locator('#print')).to_be_disabled()
        f.locator('#price0').fill('1250')
        f.locator('#save').click()
        expect(f.locator('#status')).to_contain_text('Draft saved to')
        f.locator('#print').click()
        expect(f.locator('#status')).to_contain_text('PDF created:')
        uploads = page.evaluate('uploads')
        pdf = base64.b64decode(uploads[0]['b64'])
        assert pdf.startswith(b'%PDF-1.4') and b'9329.60' in pdf
        if shutil.which('pdftotext'):
            with tempfile.TemporaryDirectory(prefix='showcase-pdf-') as tmp:
                pdf_path = Path(tmp) / 'sample.pdf'
                pdf_path.write_bytes(pdf)
                text = subprocess.check_output(['pdftotext', str(pdf_path), '-']).decode()
                assert 'Werkraum' in text and 'Rheinwerk' in text and '9329.60' in text
        page.evaluate('(p)=>files[p]+=" "', path)
        f.locator('#save').click()
        expect(f.locator('#status')).to_contain_text('Another visitor')
        page.set_viewport_size({'width': 390, 'height': 844})
        frame = page.frames[1]
        assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        print('PASS invoice: invalid inputs, save, PDF parse/totals, stale edits, narrow layout')

        rows = [
            ['company/dashboards/kanban.md', False, 'Inspect enclosure @bob #blocked (color: blue) (due: 2026-09-18)', ['blocked'], ['bob'], 3],
            ['projects/polaris-energy-gateway/meeting-notes.md', True, 'Record decision', [], ['alice'], 5],
            ['company/sales/customer-requirements.md', False, 'Template', [], [], 1],
        ]
        f = mount(page, 'company/dashboards/management-cockpit.html', rows=rows)
        expect(f.locator('#open')).to_have_text('1')
        expect(f.locator('#done')).to_have_text('1')
        expect(f.locator('#blocked')).to_have_text('1')
        expect(f.locator('#sources')).to_have_text('2')
        expect(f.locator('#work')).not_to_contain_text('(color:')
        expect(f.locator('#work')).to_contain_text('due 2026-09-18')
        f.locator('#owner').select_option('alice')
        expect(f.locator('#open')).to_have_text('0')
        expect(f.locator('#done')).to_have_text('1')
        assert not errors, errors
        print('PASS cockpit: counts, template exclusion, owner filter; no browser errors')
        browser.close()


if __name__ == '__main__':
    run()
