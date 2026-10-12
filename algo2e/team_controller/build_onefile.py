"""Rebuild the self-contained Windows launcher from the reviewed local modules."""
import hashlib
from pathlib import Path


ROOT=Path(__file__).resolve().parent
MODULES=('core','runner','test_algo2e','test_execution','menu')


def build():
    code=['import sys, types, os\nfrom pathlib import Path\n']
    code.append("if sys.version_info<(3,9):\n    raise SystemExit('Python 3.9 or newer is required')\n")
    for name in MODULES:
        data=(ROOT/(name+'.py')).read_bytes(); digest=hashlib.sha256(data).hexdigest()
        code.append('source=%r\n'%data.decode('utf-8'))
        code.append("module=types.ModuleType(%r)\nmodule.__file__=%r\nmodule.SOURCE_SHA256=%r\nsys.modules[%r]=module\nexec(compile(source,module.__file__,'exec'),module.__dict__)\n"%(name,name+'.py',digest,name))
    code.append("if __name__=='__main__':\n    sys.exit(sys.modules['menu'].main())\n")
    payload=''.join(code)
    (ROOT/'ALGO2E_ONEFILE.py').write_text(payload,encoding='utf-8')
    bootstrap="import sys; p=sys.argv[1]; sys.argv=sys.argv[1:]; exec(compile(open(p,encoding='utf-8').read().split('\\n#==ALGO2E_PYTHON==\\n',1)[1],p,'exec'))"
    invocation=' -c "'+bootstrap+'" "%~f0" %*'
    header='\n'.join([
        '@echo off','setlocal','cd /d "%~dp0"',
        'if exist "C:\\ProgramData\\Anaconda3\\python.exe" (',
        '  "C:\\ProgramData\\Anaconda3\\python.exe"'+invocation,
        '  goto :finished',')',
        'py -3 -c "import sys; assert sys.version_info >= (3,9)" >nul 2>nul',
        'if not errorlevel 1 (','  py -3'+invocation,'  goto :finished',')',
        'python -c "import sys; assert sys.version_info >= (3,9)" >nul 2>nul',
        'if not errorlevel 1 (','  python'+invocation,'  goto :finished',')',
        'echo Python 3.9 or newer was not found. Use your Anaconda Python installation.',
        ':finished','pause','exit /b','#==ALGO2E_PYTHON==',''])
    target=ROOT/'RUN_ALGO2E.bat'
    target.write_bytes((header+payload).replace('\n','\r\n').encode('utf-8'))
    print('Built',target.name,'and ALGO2E_ONEFILE.py')


if __name__=='__main__': build()
