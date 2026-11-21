"""Kolejka offline przy zmianie konta; wykonujemy rzeczywiste funkcje aplikacji."""
import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless
from django.test import SimpleTestCase


@skipUnless(shutil.which('node'), 'brak Node.js')
class SyncOwnershipTests(SimpleTestCase):
    def test_account_is_checked_before_any_write(self):
        source = (Path(__file__).resolve().parents[1] / 'static/sport/js/activity-app.js').read_text()
        apply = source[source.index('    function applyPayload('):source.index('    var syncTimer')]
        start = source.index('    function sync(retried)')
        sync = source[start:source.index('    // ---', start)]
        program = r'''
const vm = require('vm');
(async () => {
  for (const owner of [1, 2]) {
    const calls = [];
    const state = {user: 'alice', userId: 1, queue: [{id:'op', type:'goal',value:5}], errors:[]};
    const ctx = {state, Promise, Date, config:{snapshotUrl:'snapshot',syncUrl:'sync'},
      renderStatus(){}, render(){}, scheduleSync(){}, persist(){return Promise.resolve();},
      request(){calls.push('read');return Promise.resolve({ok:true,status:200,data:{user:owner===1?'alice':'bob',user_id:owner,csrf_token:'new',snapshot:{}}});},
      postJson(url, body){calls.push('write');if(body.user_id!==owner)throw Error('wrong owner');return Promise.resolve({ok:true,status:200,data:{user:'alice',user_id:owner,results:[]}});}
    };
    vm.createContext(ctx);
    vm.runInContext(FUNCTIONS,ctx);
    await ctx.sync();
    if(JSON.stringify(calls)!==JSON.stringify(owner===1?['read','write']:['read']))throw Error(JSON.stringify(calls));
  }
})();
'''.replace('FUNCTIONS', json.dumps(apply + sync))
        result = subprocess.run(['node', '-e', program], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
