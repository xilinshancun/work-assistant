"""Official lark-cli stream; bounded readers, open stdin, no restart loop."""
import os
import signal
import subprocess
import threading
import time
from .inbound_paseo import ProtocolError
from .process import subprocess_environment

EVENT = 'im.message.receive_v1'
READY = '[event] ready event_key=' + EVENT
MAX_LINE = 64 * 1024


class Consumer:
    def __init__(self,client,popen=subprocess.Popen):
        self.client=client;self.popen=popen;self.proc=None;self.ready=threading.Event()
        self.failure=None;self.threads=[]

    def start(self,on_line,on_diagnostic):
        # client identity/config/bot-info preflight is completed by the caller.
        argv=self.client.argv+['event','consume',EVENT,'--as','bot']
        env,_=subprocess_environment(self.client.environment,inherit_env=False)
        try:
            self.proc=self.popen(argv,cwd=self.client.settings['cli_config_dir'],env=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                start_new_session=True,shell=False)
        except OSError:raise ProtocolError('official event consumer could not start') from None
        def read_stdout():
            try:
                while True:
                    line=self.proc.stdout.readline(MAX_LINE+1)
                    if not line:break
                    if len(line)>MAX_LINE:
                        on_diagnostic('stdout_limit');self.failure='stdout_limit';break
                    on_line(line)  # Durable journal before reading the next event.
            except BaseException:
                self.failure='journal_reader_failure'
            finally:self.proc.stdout.close()
        def read_stderr():
            try:
                while True:
                    line=self.proc.stderr.readline(MAX_LINE+1)
                    if not line:break
                    if len(line)>MAX_LINE:
                        self.failure='stderr_limit';on_diagnostic('stderr_limit');break
                    text=line.decode('utf-8','replace').strip()
                    if text==READY:self.ready.set();on_diagnostic('ready')
                    elif 'seq gap' in text or 'dropped oldest' in text:on_diagnostic('delivery_gap')
                    else:on_diagnostic('consumer_diagnostic')  # Never retain raw stderr.
            except BaseException:self.failure='diagnostic_reader_failure'
            finally:self.proc.stderr.close()
        self.threads=[threading.Thread(target=read_stdout,daemon=True),threading.Thread(target=read_stderr,daemon=True)]
        for thread in self.threads:thread.start()

    def alive(self):return self.proc is not None and self.proc.poll() is None and self.failure is None

    def close(self):
        if self.proc is None:return
        if self.proc.poll() is None:
            try:os.killpg(self.proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:os.killpg(self.proc.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                self.proc.wait(timeout=3)
        if self.proc.stdin:self.proc.stdin.close()
        for thread in self.threads:thread.join(timeout=3)

    def wait_ready(self,timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if self.ready.wait(.1):return True
            if not self.alive():return False
        return False
