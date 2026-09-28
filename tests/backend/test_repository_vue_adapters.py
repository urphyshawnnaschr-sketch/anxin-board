"""Pure safe-text fixtures; no credentials, source execution or network."""
import json
import pytest
from app.repository_vue_adapters import TECHNOLOGIES, analyze, detect, scope_eligible, context_eligible

@pytest.mark.parametrize('technology,source', [
 ('Vue2', "import V from 'vue'; new V({render:h=>h(App)}).$mount('#app')"),
 ('VueRouter', "import R from 'vue-router'; export default new R({routes: []})"),
 ('Vuex', "import V from 'vuex'; export default new V.Store({state:{}})"),
 ('Axios', "import a from 'axios'; const service=a.create({}); service.get(endpoint)"),
 ('ElementUI', "import E from 'element-ui'; Vue.use(E)"),
])
def test_local_qualification_requires_usage(technology, source):
 assert technology in TECHNOLOGIES
 assert technology in detect('src/main.js', source)
 assert any(row['technology']==technology for row in analyze('src/main.js',source))
 assert detect('src/main.js', source.split(';')[0]+';') == set()
 assert detect('README.md', source) == set()
 assert detect('src/main.js', '/* '+source+' */') == set()
 assert detect('src/main.js', '// '+source) == set()
 assert detect('src/main.js', 'const example = '+json.dumps(source)) == set()


def test_vue2_scope_does_not_infer_vue3_or_dead_imports():
 source = '<template><order-card/><el-button>Save</el-button></template><script>import OrderCard from "./OrderCard.vue"; export default {components:{OrderCard}}</script>'
 assert detect('Panel.vue', source) == set()
 assert scope_eligible('Panel.vue', source) == {'Vue2'}
 assert scope_eligible('example.js', source) == set()
 assert scope_eligible('Panel.vue', '<!-- '+source+' -->') == set()
 assert analyze('Panel.vue', source) == []
 rows = analyze('Panel.vue', source, {'Vue2'})
 assert any(r['evidence_type']=='component_import' and r['symbol']=='OrderCard' and r['related_path']=='./OrderCard.vue' for r in rows)
 assert {r['technology'] for r in rows} == {'Vue2'}
 assert detect('main.js', "import Vue from 'vue'; Vue.createApp({})") == set()
 assert not any(r['evidence_type']=='component_import' for r in analyze('Panel.vue', '<template><div/></template><script>import Card from "./Card.vue"</script>', {'Vue2'}))


def test_route_lazy_import_store_http_ui_evidence_has_local_spans():
 fixtures = [
 ('routes.js', "import Router from 'vue-router'; new Router({routes:[{path:'/home',component:Home},{path:'/orders',name:'Orders',component:()=>import('./Orders.vue')}]})", {'VueRouter'}, {'route_path','route_name','route_component','lazy_component'}),
 ('store.js', "import Vuex from 'vuex'; new Vuex.Store({modules:{orders},state:{count:0},actions:{load(ctx){}},mutations:{set(state){}},getters:{count(state){}}}); store.dispatch('orders/load')", {'Vuex'}, {'store_section','store_call','store_member'}),
 ('http.js', "import axios from 'axios'; const service=axios.create({}); service.post(endpoint); service({url:orderEndpoint})", {'Axios'}, {'http_instance','http_call','symbolic_endpoint'}),
 ('Panel.vue', '<template><el-table/><el-button/></template><script>export default {}</script>', {'ElementUI'}, {'ui_component'}),
 ]
 for path, source, enabled, expected in fixtures:
  rows=analyze(path,source,enabled)
  assert expected <= {r['evidence_type'] for r in rows}
  for row in rows:
   assert row['adapter_version']=='1'
   assert row['symbol'] in source[row['char_start']:row['char_end']]
   assert 0 <= row['char_start'] < row['char_end'] <= len(source)
   if 'related_symbol' in row: assert row['related_symbol'] in source
   if 'related_path' in row: assert row['related_path'] in source


def test_symbolic_endpoints_do_not_emit_addresses_or_quoted_url_values():
 source="import axios from 'axios'; const service=axios.create({baseURL:'https://private.example'}); service.get('http://10.0.0.1/private'); service({url:safeEndpoint})"
 rows=analyze('http.js',source)
 output=json.dumps(rows)
 assert '10.0.0.1' not in output and 'private.example' not in output and 'http://' not in output
 assert any(r['symbol']=='safeEndpoint' for r in rows)
 assert analyze('http.js',source,enabled=set())==[]


def test_enabled_scope_does_not_turn_comments_or_unrelated_names_into_routes():
 assert analyze('data.js', "const x = {name:'Example'}", {'VueRouter'}) == []
 assert analyze('Panel.vue', '<!-- <el-table/> -->', {'ElementUI'}) == []
 assert analyze('routes.js', "/* component:Panel, path:'/orders' */", {'VueRouter'}) == []


def test_adapter_has_no_io_imports():
 import ast
 from pathlib import Path
 from app import repository_vue_adapters
 tree=ast.parse(Path(repository_vue_adapters.__file__).read_text(encoding='utf-8-sig'))
 assert [n.names[0].name for n in tree.body if isinstance(n,ast.Import)] == ['re', 'posixpath']
 assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'open','exec','eval','compile','__import__'} for n in ast.walk(tree))


def test_context_qualification_follows_explicit_router_and_http_import_usage():
 sources={
  'src/router/index.js': "import R from 'vue-router'; import entries from './data'; new R({routes:entries})",
  'src/router/data.js': "export default [{path:'/orders',name:'Orders',component:()=>import('../views/Orders.vue')}]",
  'src/http/client.js': "import axios from 'axios'; const client=axios.create({}); export default client",
  'src/api/orders.js': "import query from '@/http/client'; export function orders(){return query({url:ordersEndpoint})}",
 }
 assert context_eligible('src/router/data.js',sources['src/router/data.js'],sources)=={'VueRouter'}
 assert context_eligible('src/api/orders.js',sources['src/api/orders.js'],sources)=={'Axios'}
 assert {'route_path','route_name','lazy_component'} <= {r['evidence_type'] for r in analyze('src/router/data.js',sources['src/router/data.js'],{'VueRouter'})}
 assert {'http_wrapper_import','http_call','symbolic_endpoint'} <= {r['evidence_type'] for r in analyze('src/api/orders.js',sources['src/api/orders.js'],{'Axios'}, source_context=sources)}
 assert context_eligible('src/api/orders.js',"import query from '@/http/client';",sources)==set()
 assert context_eligible('other/src/router/data.js',sources['src/router/data.js'],sources)==set()
 assert context_eligible('src/api/orders.js',sources['src/api/orders.js'],{})==set()
 dead={**sources,'src/http/client.js':"import axios from 'axios'; export default unrelated"}
 assert context_eligible('src/api/orders.js',sources['src/api/orders.js'],dead)==set()


def test_shadowed_framework_import_is_unknown():
 assert detect('client.js', "import axios from 'axios'; function f(axios){axios.get(endpoint)}") == set()
 assert detect('client.js', "import axios from 'axios'; const f = (axios)=>axios.get(endpoint)") == set()
 assert detect('client.ts', "import axios from 'axios'; function f(axios: Client): void {axios.get(endpoint)}") == set()
 assert detect('client.js', "import axios from 'axios'; function f(obj){const {axios}=obj; axios.get(endpoint)}") == set()


def test_named_import_cannot_borrow_default_client_provenance():
 sources={'client.js':"import axios from 'axios'; const c=axios.create({}); export default c;"}
 source="import { unrelated } from './client'; unrelated({url:endpoint})"
 assert context_eligible('api.js',source,sources) == set()


def test_router_import_must_resolve_the_exported_collection():
 route="export default [{path:'/orders',component:Orders}]"
 sources={'data.js':route,'router.js':"import R from 'vue-router'; import {missing} from './data'; new R({routes:missing})"}
 assert context_eligible('data.js',route,sources) == set()
 sources['router.js']="import R from 'vue-router'; import entries from './data'; new R({routes:entries})"
 assert context_eligible('data.js',route,sources) == {'VueRouter'}
 unrelated="const unused=[{path:'/orders',component:Orders}]; export default unrelated;"
 assert context_eligible('data.js',unrelated,{**sources,'data.js':unrelated}) == set()
 local="const entries=[{path:'/orders',component:Orders}]; export default entries;"
 assert context_eligible('data.js',local,{**sources,'data.js':local}) == {'VueRouter'}
 transformed="const entries=[{path:'/orders',component:Orders}].map(x => unrelated); export default entries;"
 assert context_eligible('data.js',transformed,{**sources,'data.js':transformed}) == set()
 direct="export default [{path:'/orders',component:Orders}].map(x => unrelated);"
 assert context_eligible('data.js',direct,{**sources,'data.js':direct}) == set()
 asi="const entries=[{path:'/orders',component:Orders}]\n\nexport default entries;"
 assert context_eligible('data.js',asi,{**sources,'data.js':asi}) == {'VueRouter'}
 continued="const entries=[{path:'/orders',component:Orders}]\n.map(x => unrelated); export default entries;"
 assert context_eligible('data.js',continued,{**sources,'data.js':continued}) == set()
