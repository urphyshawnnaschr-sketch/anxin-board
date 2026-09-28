from app.repository_parsers import JavaSpringHeuristics, WebHeuristics


def _assert_literal_metadata(text, metadata):
    for values in metadata.values():
        for value in values:
            assert value in text


def test_vue_request_wrapper_extracts_literal_api_url_only():
    text = '''
import request from '@/utils/request'
export function listGroups(params) {
  return request({
    url: '/api/taskGroup/list',
    method: 'GET',
    params
  })
}
'''
    result = WebHeuristics().parse('src/api/groups.js', text)
    assert result.structured['api'] == ['/api/taskGroup/list']
    assert '@/utils/request' in result.structured['import']
    _assert_literal_metadata(text, result.structured)


def test_vue_request_wrapper_ignores_dynamic_url():
    text = '''
export function item(id) {
  return request({ url: `/api/task/${id}`, method: 'GET' })
}
'''
    result = WebHeuristics().parse('src/api/task.js', text)
    assert result.structured['api'] == []
    _assert_literal_metadata(text, result.structured)


def test_spring_parser_extracts_literal_controller_structure():
    text = '''
package com.example.camera;
import com.example.camera.service.DeviceService;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/api/device")
public class DeviceController {
    private final DeviceService service;
    @GetMapping("/list")
    public Result listDevices() { return service.list(); }
    @PostMapping(path = "/add")
    public Result addDevice() { return service.add(); }
}
'''
    result = JavaSpringHeuristics().parse('src/main/java/DeviceController.java', text)
    assert {'DeviceController', 'listDevices', 'addDevice'} <= set(result.structured['symbol'])
    assert result.structured['route'] == ['/add', '/api/device', '/list']
    assert 'com.example.camera.service.DeviceService' in result.structured['import']
    _assert_literal_metadata(text, result.structured)


def test_spring_table_annotation_is_literal_backed():
    text = '''
@TableName("camera_device")
public class DeviceEntity {}
'''
    result = JavaSpringHeuristics().parse('src/main/java/DeviceEntity.java', text)
    assert result.structured['table'] == ['camera_device']
    _assert_literal_metadata(text, result.structured)
