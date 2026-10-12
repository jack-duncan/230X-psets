"""One operator menu. Credentials remain in memory for this process only."""
import contextlib
import getpass
import io
import os
from pathlib import Path
import sys
import unittest
import runner


def self_test():
    import test_algo2e
    import test_execution
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m)
                             for m in (test_algo2e,test_execution))
    print('Running offline checks. No classroom connection or simulation orders.')
    with contextlib.redirect_stdout(io.StringIO()):
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    print('OFFLINE CHECKS PASSED' if result.wasSuccessful() else 'CHECKS FAILED: do not start trading')
    return 0 if result.wasSuccessful() else 1


def invoke(args):
    try: return runner.main(args)
    except (runner.SafetyError,runner.HTTPFailure,runner.Uncertain,ValueError,OSError) as exc:
        print('Unable to run:',exc); return 1


def main(argv=None):
    if sys.version_info<(3,9):
        print('Python 3.9 or newer is required.'); return 1
    argv=sys.argv[1:] if argv is None else argv
    if argv:
        return self_test() if argv==['--self-test'] else invoke(argv)
    runner.disable_quickedit()
    profile='balanced'; port=45325; configured=False; fee='auto'; settlement='flatten'
    profiles=('balanced','maker','trend','pairs','defensive')
    while True:
        print('\nALGO2e | ONE shared account, ONE operator')
        print('Profile:',profile,'| Client port:',port,'| End:',settlement,'| Fees:',fee)
        print('1  Check connection (read only)\n2  Observe (read only)\n3  TRADE\n4  Recover: cancel and flatten only')
        print('5  Offline self-tests\n6  Choose strategy profile\n7  Connection / fee / end settings\n0  Quit')
        try: choice=input('Choose: ').strip()
        except (EOFError,KeyboardInterrupt): print(); return 0
        if choice=='0': return 0
        if choice=='5': self_test(); continue
        if choice=='6':
            print('Profiles:',', '.join(profiles))
            value=input('Profile [balanced]: ').strip() or 'balanced'
            if value in profiles: profile=value
            else: print('Unknown profile; unchanged.')
            continue
        if choice=='7':
            print('Use auto fees and flatten unless the instructor confirms otherwise.')
            value=input('Fee convention [auto / net / offset], Enter keeps current: ').strip()
            if value in ('auto','net','offset'): fee=value
            value=input('End policy [flatten / net], Enter keeps current: ').strip()
            if value in ('flatten','net'): settlement=value
            configured=False
            if input('Replace saved-in-memory key? [y/N]: ').lower()=='y': os.environ.pop('RIT_API_KEY',None)
            continue
        if choice not in ('1','2','3','4'): continue
        try:
            if not configured:
                value=input('CLIENT REST Port [%s]: '%port).strip()
                candidate=int(value) if value else port
                if not 1<=candidate<=65535: raise ValueError('Port must be 1 to 65535')
                port=candidate
                if not os.environ.get('RIT_API_KEY'):
                    key=getpass.getpass('CLIENT REST API key (hidden): ').strip()
                    if not key: raise ValueError('API key cannot be blank')
                    os.environ['RIT_API_KEY']=key
                configured=True
            args=['--base-url','http://localhost:%s/v1'%port,'--profile',profile,
                  '--fee-mode',fee,'--settlement',settlement]
            args+= {'1':[],'2':['--observe'],'3':['--trade'],'4':['--flatten']}[choice]
            if Path('settings.json').is_file(): args+=['--config','settings.json']
            if choice in ('3','4'):
                print('One writer only. Check RIT Client after any halt. Ctrl+C requests a controlled stop.')
            invoke(args)
        except (ValueError,EOFError,KeyboardInterrupt) as exc:
            print('Menu action stopped:',exc)


if __name__=='__main__': sys.exit(main())
