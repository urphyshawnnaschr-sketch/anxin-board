"""Compile and exercise native launcher with isolated synthetic lifecycle scripts."""
from pathlib import Path
import os
import struct
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[2]
WINDOWS=ROOT/"installer/windows"


def test_inno_owns_desktop_start_menu_and_setup_icons():
    text=(WINDOWS/"AnxinBoard.iss").read_text(encoding="utf-8-sig")
    for target in ('{autodesktop}', '{group}'):
        entry=next(line for line in text.splitlines() if line.startswith('Name: "'+target+'\\安心看板";'))
        assert 'Filename: "{app}\\安心看板.exe"' in entry
        assert 'IconFilename: "{app}\\AnxinBoard.ico"' in entry
    assert 'SetupIconFile={#LauncherRoot}\\AnxinBoard.ico' in text
    assert 'UninstallDisplayIcon={app}\\安心看板.exe' in text
    for label in ('退出','恢复备份'):
        entry=next(line for line in text.splitlines() if 'Name: "{group}\\安心看板 - '+label+'"' in line)
        assert 'IconFilename: "{app}\\AnxinBoard.ico"' in entry
    build=(WINDOWS/"build-installer.ps1").read_text(encoding="utf-8-sig")
    assert 'build-launcher.ps1' in build and '/DLauncherRoot=' in build


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if os.name!="nt":pytest.skip("Windows compiler")
    out=tmp_path_factory.mktemp("native-launcher")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(WINDOWS/'build-launcher.ps1'),'-OutputRoot',str(out)],capture_output=True,text=True,timeout=45)
    assert result.returncode==0, result.stdout+result.stderr
    csc=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    probe=out/'Probe.cs'
    probe.write_text('public static class Probe { public static int Main(string[] args) { var result = AnxinBoard.DesktopLauncher.Run(args[0], int.Parse(args[1])); System.Console.WriteLine(result); return 0; } }')
    result=subprocess.run([str(csc),'/nologo','/target:exe','/main:Probe','/r:System.Windows.Forms.dll','/out:'+str(out/'probe.exe'),str(WINDOWS/'AnxinBoard.Launcher.cs'),str(probe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    return out


def test_real_launcher_has_gui_subsystem_and_multisize_icon(built):
    raw=(built/'安心看板.exe').read_bytes();pe=struct.unpack_from('<I',raw,0x3c)[0]
    assert raw[pe:pe+4]==b'PE\0\0'
    assert struct.unpack_from('<H',raw,pe+24+68)[0]==2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
    ico=(built/'AnxinBoard.ico').read_bytes()
    sizes=[16,24,32,48,64,128,256]
    assert struct.unpack_from('<HHH',ico)==(0,1,len(sizes))
    assert ico==(WINDOWS/'assets/AnxinBoard.ico').read_bytes()
    for index,size in enumerate(sizes):
        entry=6+index*16
        assert ico[entry:entry+2]==bytes([size%256,size%256])
        length,offset=struct.unpack_from('<II',ico,entry+8)
        assert offset>=6+16*len(sizes) and offset+length<=len(ico)
        png=ico[offset:offset+length]
        assert png[:8]==b'\x89PNG\r\n\x1a\n'
        assert struct.unpack_from('>II',png,16)==(size,size)


@pytest.mark.parametrize('script,expected',[
    ("$expected=Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\Modules'; if ($env:PSModulePath -ne $expected) { exit 31 }; Write-Output 'private-body'; [Console]::Error.WriteLine('private-secret'); exit 0",'OK'),
    ("[Console]::Error.WriteLine('private-secret'); exit 37",'LAUNCH_SCRIPT_FAILED:37'),
    ("throw 'private-secret'",'LAUNCH_SCRIPT_FAILED:1'),
    (None,'LAUNCH_FILES_MISSING'),
])
def test_launcher_fixed_environment_safe_failures_and_no_raw_output(built,tmp_path,script,expected):
    tools=tmp_path/'tools';tools.mkdir()
    if script is not None:(tools/'start-installed-product.ps1').write_text(script,encoding="utf-8-sig")
    env=dict(os.environ,PSModulePath='synthetic-untrusted-module-path')
    run=subprocess.run([str(built/'probe.exe'),str(tmp_path),'10000'],env=env,capture_output=True,text=True,timeout=15)
    assert run.returncode==0 and run.stdout.strip()==expected
    assert not run.stderr and 'private' not in run.stdout


def test_launcher_timeout_reports_without_killing_child(built,tmp_path):
    tools=tmp_path/'tools';tools.mkdir()
    done=tmp_path/'done'
    (tools/'start-installed-product.ps1').write_text("Start-Sleep -Milliseconds 250; Set-Content -LiteralPath '"+str(done).replace("'","''")+"' -Value 'synthetic'",encoding="utf-8-sig")
    run=subprocess.run([str(built/'probe.exe'),str(tmp_path),'1'],capture_output=True,text=True,timeout=10)
    assert run.stdout.strip()=='LAUNCH_TIMEOUT'
    import time
    deadline=time.monotonic()+5
    while not done.exists() and time.monotonic()<deadline:time.sleep(.05)
    assert done.read_text().strip()=='synthetic'


def test_both_streams_drain_large_unbroken_output_without_leak(built,tmp_path):
    tools=tmp_path/'tools';tools.mkdir()
    (tools/'start-installed-product.ps1').write_text("[Console]::Out.Write(('x' * 262144)); [Console]::Error.Write(('y' * 262144)); exit 0")
    run=subprocess.run([str(built/'probe.exe'),str(tmp_path),'10000'],capture_output=True,text=True,timeout=15)
    assert run.stdout.strip()=='OK' and not run.stderr


def test_production_winexe_invokes_only_owned_script_and_exits_quietly(built,tmp_path):
    import shutil
    exe=tmp_path/'安心看板.exe';shutil.copyfile(built/'安心看板.exe',exe)
    tools=tmp_path/'tools';tools.mkdir()
    marker=tools/'called'
    (tools/'start-installed-product.ps1').write_text("Set-Content -LiteralPath '"+str(marker).replace("'","''")+"' -Value 'synthetic'; exit 0",encoding='utf-8-sig')
    result=subprocess.run([str(exe)],capture_output=True,timeout=15)
    assert result.returncode==0 and not result.stdout and not result.stderr
    assert marker.read_text().strip()=='synthetic'


def test_real_inno_compiles_new_icon_and_executable_entries(built,tmp_path):
    import shutil
    compiler=shutil.which('ISCC.exe') or str(Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Inno Setup 6/ISCC.exe')
    if not Path(compiler).is_file():pytest.skip('Inno compiler unavailable')
    payload=tmp_path/'payload';(payload/'AnxinBoard.Runtime').mkdir(parents=True)
    (payload/'AnxinBoard.Runtime/synthetic.txt').write_text('synthetic')
    (payload/'runtime-manifest.json').write_text('{}')
    result=subprocess.run([compiler,'/Q','/DPayloadRoot='+str(payload),'/DLauncherRoot='+str(built),'/DOutputRoot='+str(tmp_path/'out'),'/DSourceCommit='+'a'*40,'/DSourceCommitShort='+'a'*12,str(WINDOWS/'AnxinBoard.iss')],capture_output=True,timeout=60)
    assert result.returncode==0,result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace')


def test_owned_script_path_quotes_are_data_not_powershell_code(built,tmp_path):
    root=tmp_path/"name' ; throw 'private' ; $('quoted')"
    tools=root/'tools';tools.mkdir(parents=True)
    (tools/'start-installed-product.ps1').write_text('exit 0')
    result=subprocess.run([str(built/'probe.exe'),str(root),'10000'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and result.stdout.strip()=='OK' and not result.stderr


@pytest.mark.parametrize('code',['LOCAL_SESSION_HANDOFF_UNAVAILABLE','LOCAL_SESSION_HANDOFF_START_FAILED'])
def test_only_allowlisted_handoff_diagnostic_survives_sensitive_output(built,tmp_path,code):
    tools=tmp_path/'tools';tools.mkdir()
    # Cross the drain-buffer boundary; never retain the sensitive tail or raw exception.
    (tools/'start-installed-product.ps1').write_text("[Console]::Out.Write(('x' * 4085)); throw '"+code+": private-secret-token-and-body'",encoding='utf-8-sig')
    result=subprocess.run([str(built/'probe.exe'),str(tmp_path),'10000'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and result.stdout.strip()==code and not result.stderr
    assert 'private' not in result.stdout


def test_nonallowlisted_code_is_not_exposed(built,tmp_path):
    tools=tmp_path/'tools';tools.mkdir()
    (tools/'start-installed-product.ps1').write_text("throw 'PRIVATE_UNKNOWN_SECRET_CODE: private-secret'",encoding='utf-8-sig')
    result=subprocess.run([str(built/'probe.exe'),str(tmp_path),'10000'],capture_output=True,text=True,timeout=15)
    assert result.stdout.strip()=='LAUNCH_SCRIPT_FAILED:1' and not result.stderr
