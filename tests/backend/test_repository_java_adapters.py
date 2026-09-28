import pytest

from app.repository_java_adapters import TECHNOLOGIES, analyze, detect


def checked(source, enabled=None):
    rows = analyze("src/Example.java", source, enabled)
    for row in rows:
        assert row["technology"] in TECHNOLOGIES
        assert row["adapter_version"] == "1"
        assert 0 <= row["char_start"] < row["char_end"] <= len(source)
        assert row["symbol"] in source[row["char_start"]:row["char_end"]]
        if "related_symbol" in row:
            assert row["related_symbol"] in source
    return rows


def test_boot_qualified_annotations_and_real_entrypoint():
    source = '''import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.SpringApplication;
@SpringBootApplication public class Application {
 public static void main(String[] args) { SpringApplication.run(Application.class, args); }
}'''
    rows = checked(source)
    assert detect("Application.java", source) == {"SpringBoot"}
    assert {r["relation"] for r in rows} == {"boot_application", "application_entrypoint"}


def test_spring_mvc_routes_di_and_service_dependency():
    source = '''import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.beans.factory.annotation.Autowired;
@RestController @RequestMapping(path = "/orders")
public class OrderController {
 @Autowired private OrderService orderService;
 @GetMapping(value = {"/list", "/all"}, produces = {"application/json", "application/xml"})
 public String list() { return orderService.list(); }
}'''
    rows = checked(source)
    assert detect("OrderController.java", source) == {"SpringMVC", "SpringDI"}
    routes = [(r["relation"], r["symbol"], r["related_symbol"]) for r in rows if r["evidence_type"] == "route_declaration"]
    assert routes == [("base_route", "/orders", "OrderController"), ("method_route", "/all", "list"), ("method_route", "/list", "list")]
    assert any(r["relation"] == "injected_type" and r["related_symbol"] == "OrderService" for r in rows)
    assert any(r["relation"] == "controller_dependency" and r["symbol"] == "orderService" for r in rows)
    assert all("application/" not in r["symbol"] for r in rows)


def test_mybatis_mapper_scan_and_param_wiring():
    source = '''import org.mybatis.spring.annotation.MapperScan;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
@MapperScan(basePackages = {"app.mapper", "app.extra"}, sqlSessionFactoryRef="factory")
class Application {}
@Mapper interface AccountMapper {
 Account find(@Param("accountId") Long id);
}'''
    rows = checked(source)
    assert detect("Mapper.java", source) == {"MyBatis"}
    assert {r["symbol"] for r in rows if r["relation"] == "mapper_package"} == {"app.mapper", "app.extra"}
    assert any(r["symbol"] == "accountId" and r["related_symbol"] == "id" for r in rows)


@pytest.mark.parametrize("imported, declaration, relation, related", [
    ("com.baomidou.mybatisplus.core.mapper.BaseMapper", "interface AccountMapper extends BaseMapper<Account> {}", "maps_entity", "Account"),
    ("com.baomidou.mybatisplus.extension.service.IService", "interface AccountService extends IService<Account> {}", "service_entity", "Account"),
    ("com.baomidou.mybatisplus.extension.service.impl.ServiceImpl", "class AccountServiceImpl extends ServiceImpl<AccountMapper, Account> {}", "service_mapper", "AccountMapper"),
])
def test_mybatis_plus_generic_contracts(imported, declaration, relation, related):
    source = f"import {imported};\n{declaration}"
    rows = checked(source)
    assert detect("Account.java", source) == {"MyBatisPlus"}
    assert any(r["relation"] == relation and r["related_symbol"] == related for r in rows)


def test_service_impl_also_binds_entity():
    source = "import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl; class S extends ServiceImpl<Mapper, Entity> {}"
    rows = checked(source)
    assert {(r["relation"], r["related_symbol"]) for r in rows} == {("service_mapper", "Mapper"), ("service_entity", "Entity")}


@pytest.mark.parametrize("source", [
    "import org.springframework.stereotype.Controller; class Plain {}",
    "import com.baomidou.mybatisplus.core.mapper.BaseMapper; interface Plain {}",
    "import org.apache.ibatis.annotations.Mapper; interface Plain {}",
    "@Controller class Custom {}",
    "import local.Controller; @Controller class Custom {}",
    "import org.springframework.stereotype.Controller; @interface Controller {} @Controller class Custom {}",
    "import local.BaseMapper; interface MyMapper extends BaseMapper<Entity> {}",
    "// import org.springframework.stereotype.Controller;\n// @Controller class Fake {}",
    'class Plain { String fake = "import org.springframework.stereotype.Controller; @Controller class Fake {}"; }',
    'class Plain { String fake = """\nimport org.apache.ibatis.annotations.Mapper;\n@Mapper interface Fake {}\n"""; }',
])
def test_no_dead_import_shadow_comment_or_string_false_positives(source):
    assert checked(source) == []
    assert detect("Plain.java", source) == set()


def test_annotation_in_comment_does_not_qualify_import():
    source = "import org.springframework.stereotype.Service; /* @Service */ public class S {}"
    assert checked(source) == []


def test_enabled_gate_and_non_java_paths():
    source = "import org.springframework.stereotype.Service; @Service public class S {}"
    assert checked(source, enabled=set()) == []
    assert checked(source, enabled={"MyBatis"}) == []
    assert len(checked(source, enabled={"SpringDI"})) == 1
    assert analyze("README.md", source) == []
    assert detect("source.txt", source) == set()


def test_qualified_annotation_and_unambiguous_wildcard_only():
    source = "@org.springframework.stereotype.Service public class S {}"
    assert detect("S.java", source) == {"SpringDI"}
    source = "import org.springframework.stereotype.*; @Service public class S {}"
    assert detect("S.java", source) == {"SpringDI"}
    source = "import org.springframework.stereotype.*; import local.*; @Service public class S {}"
    assert detect("S.java", source) == set()


def test_injected_field_outside_controller_is_not_controller_dependency():
    source = '''import org.springframework.web.bind.annotation.RestController;
import org.springframework.beans.factory.annotation.Autowired;
@RestController class C {}
class Other { @Autowired private OrderService service; public void run() { service.run(); } }'''
    rows = checked(source)
    assert any(r["relation"] == "injected_type" for r in rows)
    assert not any(r["relation"] == "controller_dependency" for r in rows)


def test_comments_cannot_supply_path_literals_or_calls():
    source = '''import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.boot.SpringApplication;
class C { @GetMapping(/* "/secret" */) public String route() { return "SpringApplication.run(C.class, args)"; } }'''
    rows = checked(source)
    assert {r["relation"] for r in rows} == {"route_annotation"}
