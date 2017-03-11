# This file is Copyright (c) 2010 by the GPSD project
# BSD terms apply: see the file COPYING in the distribution root for details.
"""
gpsfake.py -- classes for creating a controlled test environment around gpsd.

The gpsfake(1) regression tester shipped with GPSD is a trivial wrapper
around this code.  For a more interesting usage example, see the
valgrind-audit script shipped with the GPSD code.

To use this code, start by instantiating a TestSession class.  Use the
prefix argument if you want to run the daemon under some kind of run-time
monitor like valgrind or gdb.  Here are some particularly useful possibilities:

valgrind --tool=memcheck --gen-suppressions=yes --leak-check=yes
    Run under Valgrind, checking for malloc errors and memory leaks.

xterm -e gdb -tui --args
    Run under gdb, controlled from a new xterm.

You can use the options argument to pass in daemon options; normally you will
use this to set the debug-logging level.

On initialization, the test object spawns an instance of gpsd with no
devices or clients attached, connected to a control socket.

TestSession has methods to attach and detch fake GPSes. The
TestSession class simulates GPS devices for you with objects composed
from a pty and a class instance that cycles sentences into the master side
from some specified logfile; gpsd reads the slave side.  A fake GPS is
identified by the string naming its slave device.

TestSession also has methods to start and end client sessions.  Daemon
responses to a client are fed to a hook function which, by default,
discards them.  You can change the hook to sys.stdout.write() to dump
responses to standard output (this is what the gpsfake executable
does) or do something more exotic. A client session is identified by a
small integer that counts the number of client session starts.

There are a couple of convenience methods.  TestSession.wait() does nothing,
allowing a specified number of seconds to elapse.  TestSession.send()
ships commands to an open client session.

TestSession does not currently capture the daemon's log output.  It is
run with -N, so the output will go to stderr (along with, for example,
Valgrind notifications).

Each FakeGPS instance tries to packetize the data from the logfile it
is initialized with. It uses the same packet-getter as the daemon.
Exception: if there is a Delay-Cookie line in a header comment, that
delimiter is used to split up the test load.

The TestSession code maintains a run queue of FakeGPS and gps.gs
(client- session) objects. It repeatedly cycles through the run queue.
For each client session object in the queue, it tries to read data
from gpsd.  For each fake GPS, it sends one line or packet of stored
data.  When a fake-GPS's go predicate becomes false, the fake GPS is
removed from the run queue.

There are two ways to use this code.  The more deterministic is
non-threaded mode: set up your client sessions and fake GPS devices,
then call the run() method.  The run() method will terminate when
there are no more objects in the run queue.  Note, you must have
created at least one fake client or fake GPS before calling run(),
otherwise it will terminate immediately.

To allow for adding and removing clients while the test is running,
run in threaded mode by calling the start() method.  This simply calls
the run method in a subthread, with locking of critical regions.
"""
import os, sys, time, signal, pty, termios # fcntl, array, struct
import exceptions, threading, socket, select
import gps
import packet as sniffer
import stat
import Queue

# The magic number below has to be derived from observation.  If
# it's too high you'll slow the tests down a lot.  If it's too low
# you'll get regression tests timing out.

# WRITE_PAD: Define a per-line delay on writes so we won't spam the
# buffers in the pty layer or gpsd itself. Values smaller than the
# system timer tick don't make any difference here. Can be set from
# WRITE_PAD in the environment.

if sys.platform.startswith("linux"):
    WRITE_PAD = 0.0
elif sys.platform.startswith("freebsd"):
    WRITE_PAD = 0.01
elif sys.platform.startswith("netbsd5"):
    WRITE_PAD = 0.200
elif sys.platform.startswith("netbsd"):
    WRITE_PAD = 0.004
elif sys.platform.startswith("darwin"):
    # darwin Darwin-13.4.0-x86_64-i386-64bit
    WRITE_PAD = 0.03
else:
    WRITE_PAD = 0.004

# Make it easier to test pad values
if os.getenv("WRITE_PAD"):
    WRITE_PAD = eval(os.getenv("WRITE_PAD"))

# Additional delays in slow mode
WRITE_PAD_SLOWDOWN = 0.01

# If a test takes longer than this, we deem it to have timed out
TEST_TIMEOUT = 60

class TestLoadError(exceptions.Exception):
    def __init__(self, msg):
        exceptions.Exception.__init__(self)
        self.msg = msg

class TestLoad:
    "Digest a logfile into a list of sentences we can cycle through."
    def __init__(self, logfp, predump=False, slow=False, oneshot=False):
        self.sentences = []	# This is the interesting part
        if type(logfp) == type(""):
            logfp = open(logfp, "r")
        self.name = logfp.name
        self.logfp = logfp
        self.predump = predump
        self.type = None
        self.sourcetype = "pty"
        self.serial = None
        self.delay = WRITE_PAD
        if slow:
            self.delay += WRITE_PAD_SLOWDOWN
        self.delimiter = None
        # Stash away a copy in case we need to resplit
        text = logfp.read()

        logfp = open(logfp.name)
        # Grab the packets in the normal way
        getter = sniffer.new()
        #gps.packet.register_report(reporter)
        type_latch = None
        commentlen = 0
        while True:
            (plen, ptype, packet, _counter) = getter.get(logfp.fileno())
            if plen <= 0:
                break
            elif ptype == sniffer.COMMENT_PACKET:
                commentlen += len(packet)
                # Some comments are magic
                if "Serial:" in packet:
                    # Change serial parameters
                    packet = packet[1:].strip()
                    try:
                        (_xx, baud, params) = packet.split()
                        baud = int(baud)
                        if params[0] in ('7', '8'):
                            databits = int(params[0])
                        else:
                            raise ValueError
                        if params[1] in ('N', 'O', 'E'):
                            parity = params[1]
                        else:
                            raise ValueError
                        if params[2] in ('1', '2'):
                            stopbits = int(params[2])
                        else:
                            raise ValueError
                    except (ValueError, IndexError):
                        raise TestLoadError("bad serial-parameter spec in %s"%\
                                            self.name)
                    self.serial = (baud, databits, parity, stopbits)
                elif "Transport: UDP" in packet:
                    self.sourcetype = "UDP"
                elif "Transport: TCP" in packet:
                    self.sourcetype = "TCP"
                elif "Delay-Cookie:" in packet:
                    if packet.startswith("#"):
                        packet = packet[1:]
                    try:
                        (_dummy, self.delimiter, delay) = packet.strip().split()
                        self.delay = float(delay)
                    except ValueError:
                        raise TestLoadError("bad Delay-Cookie line in %s"%\
                                            self.name)
                    self.resplit = True
            else:
                if type_latch is None:
                    type_latch = ptype
                if self.predump:
                    print repr(packet)
                if not packet:
                    raise TestLoadError("zero-length packet from %s"%\
                                        self.name)
                self.sentences.append(packet)
        # Look at the first packet to grok the GPS type
        self.textual = (type_latch == sniffer.NMEA_PACKET)
        if self.textual:
            self.legend = "gpsfake: line %d: "
        else:
            self.legend = "gpsfake: packet %d"
        # Maybe this needs to be split on different delimiters?
        if self.delimiter is not None:
            self.sentences = text[commentlen:].split(self.delimiter)
        # Do we want single-shot operation?
        if oneshot:
            self.sentences.append("# EOF\n")

class PacketError(exceptions.Exception):
    def __init__(self, msg):
        exceptions.Exception.__init__(self)
        self.msg = msg

class FakeGPS:
    def __init__(self, testload, progress=None):
        self.testload = testload
        self.progress = progress
        self.go_predicate = lambda: True
        self.readers = 0
        self.index = 0
        self.progress("gpsfake: %s provides %d sentences\n" % (self.testload.name, len(self.testload.sentences)))

    def write(self, line):
        "Throw an error if this superclass is ever instantiated."
        raise ValueError, line

    def feed(self, stop, manual):
        "Feed a line from the contents of the GPS log to the daemon."
        line = self.testload.sentences[self.index % len(self.testload.sentences)]
        if "%Delay:" in line:
            # Delay specified number of seconds
            delay = line.split()[1]
            time.sleep(int(delay))
        # self.write has to be set by the derived class
        self.write(line)
        time.sleep(self.testload.delay)
        ##print "Feed stop?  %r" % stop
        if not stop and not manual:
            self.index += 1

class FakePTY(FakeGPS):
    "A FakePTY is a pty with a test log ready to be cycled to it."
    def __init__(self, testload,
                 speed=4800, databits=8, parity='N', stopbits=1,
                 progress=None):
        FakeGPS.__init__(self, testload, progress)
        # Allow Serial: header to be overridden by explicit speed.
        if self.testload.serial:
            (speed, databits, parity, stopbits) = self.testload.serial
        self.speed = speed
        baudrates = {
            0: termios.B0,
            50: termios.B50,
            75: termios.B75,
            110: termios.B110,
            134: termios.B134,
            150: termios.B150,
            200: termios.B200,
            300: termios.B300,
            600: termios.B600,
            1200: termios.B1200,
            1800: termios.B1800,
            2400: termios.B2400,
            4800: termios.B4800,
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
            230400: termios.B230400,
        }
        (self.fd, self.slave_fd) = pty.openpty()
        self.byname = os.ttyname(self.slave_fd)
        os.chmod( self.byname,  stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH )
        (iflag, oflag, cflag, lflag, ispeed, ospeed, cc) = termios.tcgetattr(self.slave_fd)
        cc[termios.VMIN] = 1
        cflag &= ~(termios.PARENB | termios.PARODD | termios.CRTSCTS)
        cflag |= termios.CREAD | termios.CLOCAL
        iflag = oflag = lflag = 0
        iflag &=~ (termios.PARMRK | termios.INPCK)
        cflag &=~ (termios.CSIZE | termios.CSTOPB | termios.PARENB | termios.PARODD)
        if databits == 7:
            cflag |= termios.CS7
        else:
            cflag |= termios.CS8
        if stopbits == 2:
            cflag |= termios.CSTOPB
        # Warning: attempting to set parity makes Fedora lose its cookies
        if parity == 'E':
            iflag |= termios.INPCK
            cflag |= termios.PARENB
        elif parity == 'O':
            iflag |= termios.INPCK
            cflag |= termios.PARENB | termios.PARODD
        ispeed = ospeed = baudrates[speed]
        try:
            termios.tcsetattr(self.slave_fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
        except termios.error:
            raise TestLoadError("error attempting to set serial mode to %s %s%s%s" \
                                % (speed, databits, parity, stopbits))
    def read(self):
        "Discard control strings written by gpsd."
        # A tcflush implementation works on Linux but fails on OpenBSD 4.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        # Alas, the FIONREAD version also works on Linux and fails on OpenBSD.
        #try:
        #    buf = array.array('i', [0])
        #    fcntl.ioctl(self.master_fd, termios.FIONREAD, buf, True)
        #    n = struct.unpack('i', buf)[0]
        #    os.read(self.master_fd, n)
        #except IOError:
        #    pass

    def write(self, line):
        self.progress("gpsfake: %s writes %d=%s\n" % (self.testload.name, len(line), repr(line)))
        os.write(self.fd, line)

    def drain(self):
        "Wait for the associated device to drain (e.g. before closing)."
        termios.tcdrain(self.fd)

def cleansocket(host, port):
    "Get a socket that we can re-use cleanly after it's closed."
    cs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # This magic prevents "Address already in use" errors after
    # we release the socket.
    cs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    cs.bind((host, port))
    return cs

class FakeTCP(FakeGPS):
    "A TCP serverlet with a test log ready to be cycled to it."
    def __init__(self, testload,
                 host, port,
                 progress=None):
        FakeGPS.__init__(self, testload, progress)
        self.host = host
        self.port = int(port)
        self.byname = "tcp://" + host + ":" + str(port)
        self.dispatcher = cleansocket(self.host, self.port)
        self.dispatcher.listen(5)
        self.readables = [self.dispatcher]

    def read(self):
        "Handle connection requests and data."
        readable, _writable, _errored = select.select(self.readables, [], [], 0)
        for s in readable:
            if s == self.dispatcher:	# Connection request
                client_socket, _address = s.accept()
                self.readables = [client_socket]
                self.dispatcher.close()
            else:			# Incoming data
                data = s.recv(1024)
                if not data:
                    s.close()
                    self.readables.remove(s)

    def write(self, line):
        "Send the next log packet to everybody connected."
        self.progress("gpsfake: %s writes %d=%s\n" % (self.testload.name, len(line), repr(line)))
        for s in self.readables:
            if s != self.dispatcher:
                s.send(line)

    def drain(self):
        "Wait for the associated device(s) to drain (e.g. before closing)."
        for s in self.readables:
            if s != self.dispatcher:
                s.shutdown(socket.SHUT_RDWR)

class FakeUDP(FakeGPS):
    "A UDP broadcaster with a test log ready to be cycled to it."
    def __init__(self, testload,
                 ipaddr, port,
                 progress=None):
        FakeGPS.__init__(self, testload, progress)
        self.ipaddr = ipaddr
        self.port = port
        self.byname = "udp://" + ipaddr + ":" + str(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def read(self):
        "Discard control strings written by gpsd."
        pass

    def write(self, line):
        self.progress("gpsfake: %s writes %d=%s\n" % (self.testload.name, len(line), repr(line)))
        self.sock.sendto(line, (self.ipaddr, int(self.port)))

    def drain(self):
        "Wait for the associated device to drain (e.g. before closing)."
        pass	# shutdown() fails on UDP

class DaemonError(exceptions.Exception):
    def __init__(self, msg):
        exceptions.Exception.__init__(self)
        self.msg = msg
    def __str__(self):
        return repr(self.msg)

class DaemonInstance:
    "Control a gpsd instance."
    def __init__(self, control_socket=None):
        self.sockfile = None
        self.pid = None
        self.tmpdir = os.environ.get('TMPDIR', '/tmp')
        if control_socket:
            self.control_socket = control_socket
        else:
            self.control_socket = "%s/gpsfake-%d.sock" % (self.tmpdir, os.getpid())
        self.pidfile = "%s/gpsfake-%d.pid" % (self.tmpdir, os.getpid())
    def spawn(self, options, port, background=False, prefix=""):
        "Spawn a daemon instance."
        self.spawncmd = None

	# Look for gpsd in GPSD_HOME env variable
        if os.environ.get('GPSD_HOME'):
            for path in os.environ['GPSD_HOME'].split(':'):
                _spawncmd = "%s/gpsd" % path
                if os.path.isfile(_spawncmd) and os.access(_spawncmd, os.X_OK):
                    self.spawncmd = _spawncmd
                    break

	# if we could not find it yet try PATH env variable for it
        if not self.spawncmd:
            if not '/usr/sbin' in os.environ['PATH']:
                os.environ['PATH']=os.environ['PATH'] + ":/usr/sbin"
            for path in os.environ['PATH'].split(':'):
                _spawncmd = "%s/gpsd" % path
                if os.path.isfile(_spawncmd) and os.access(_spawncmd, os.X_OK):
                    self.spawncmd = _spawncmd
                    break

        if not self.spawncmd:
            raise DaemonError("Cannot execute gpsd: executable not found. Set GPSD_HOME env variable")
        # The -b option to suppress hanging on probe returns is needed to cope
        # with OpenBSD (and possibly other non-Linux systems) that don't support
        # anything we can use to implement the FakeGPS.read() method
        self.spawncmd += " -b -N -S %s -F %s -P %s %s" % (port, self.control_socket, self.pidfile, options)
        if prefix:
            self.spawncmd = prefix + " " + self.spawncmd.strip()
        if background:
            self.spawncmd += " &"
        status = os.system(self.spawncmd)
        if os.WIFSIGNALED(status) or os.WEXITSTATUS(status):
            raise DaemonError("daemon exited with status %d" % status)
    def wait_pid(self):
        "Wait for the daemon, get its PID and a control-socket connection."
        while True:
            try:
                fp = open(self.pidfile)
            except IOError:
                time.sleep(0.1)
                continue
            try:
                fp.seek(0)
                pidstr = fp.read()
                self.pid = int(pidstr)
            except ValueError:
                time.sleep(0.5)
                continue	# Avoid race condition -- PID not yet written
            fp.close()
            break
    def __get_control_socket(self):
        # Now we know it's running, get a connection to the control socket.
        if not os.path.exists(self.control_socket):
            return None
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
            self.sock.connect(self.control_socket)
        except socket.error:
            if self.sock:
                self.sock.close()
            self.sock = None
        return self.sock
    def is_alive(self):
        "Is the daemon still alive?"
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False
    def add_device(self, path):
        "Add a device to the daemon's internal search list."
        if self.__get_control_socket():
            self.sock.sendall("+%s\r\n\x00" % path)
            self.sock.recv(12)
            self.sock.close()
    def remove_device(self, path):
        "Remove a device from the daemon's internal search list."
        if self.__get_control_socket():
            self.sock.sendall("-%s\r\n\x00" % path)
            self.sock.recv(12)
            self.sock.close()
    def kill(self):
        "Kill the daemon instance."
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                # Raises an OSError for ESRCH when we've killed it.
                while True:
                    os.kill(self.pid, signal.SIGTERM)
                    time.sleep(0.01)
            except OSError:
                pass
            self.pid = None

class TestSessionError(exceptions.Exception):
    def __init__(self, msg):
        exceptions.Exception.__init__(self)
        self.msg = msg

class TestSession:
    "Manage a session including a daemon with fake GPSes and clients."
    def __init__(self, prefix=None, port=None, options=None, verbose=0, predump=False, udp=False, tcp=False, slow=False, queue=None):
        "Initialize the test session by launching the daemon."
        self.prefix = prefix
        self.port = port
        self.options = options
        self.verbose = verbose
        self.predump = predump
        self.udp = udp
        self.tcp = tcp
        self.slow = slow
        self.daemon = DaemonInstance()
        self.fakegpslist = {}
        self.client_id = 0
        self.readers = 0
        self.writers = 0
        self.runqueue = []
        self.index = 0
        self.baseport = 49194	# In the IANA orivate port range
        if port:
            self.port = port
        else:
            # Magic way to get a socket with an unused port number
            s = cleansocket("localhost", 0)
            self.port = s.getsockname()[1]
            s.close()
        self.progress = lambda x: None
        self.reporter = lambda x: None
        self.default_predicate = None
        self.fd_set = []
        self.threadlock = None

        self.queue = queue
        self.stop = False
        self.manual = False


    def spawn(self):
        for sig in (signal.SIGQUIT, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda unused, dummy: self.cleanup())
        self.daemon.spawn(background=True, prefix=self.prefix, port=self.port, options=self.options)
        self.daemon.wait_pid()
    def set_predicate(self, pred):
        "Set a default go predicate for the session."
        self.default_predicate = pred
    def gps_add(self, logfile, speed=19200, pred=None, oneshot=False):
        "Add a simulated GPS being fed by the specified logfile."
        self.progress("gpsfake: gps_add(%s, %d)\n" % (logfile, speed))
        if logfile not in self.fakegpslist:
            testload = TestLoad(logfile, predump=self.predump, slow=self.slow, oneshot=oneshot)
            if testload.sourcetype == "UDP" or self.udp:
                newgps = FakeUDP(testload, ipaddr="127.0.0.1",
                                 port=self.baseport,
                                 progress=self.progress)
                self.baseport += 1
            elif testload.sourcetype == "TCP" or self.tcp:
                newgps = FakeTCP(testload, host="127.0.0.1",
                                 port=self.baseport,
                                 progress=self.progress)
                self.baseport += 1
            else:
                newgps = FakePTY(testload, speed=speed,
                                   progress=self.progress)
            if pred:
                newgps.go_predicate = pred
            elif self.default_predicate:
                newgps.go_predicate = self.default_predicate
            self.fakegpslist[newgps.byname] = newgps
            self.append(newgps)
            newgps.exhausted = 0
        self.daemon.add_device(newgps.byname)
        return newgps.byname
    def gps_remove(self, name):
        "Remove a simulated GPS from the daemon's search list."
        self.progress("gpsfake: gps_remove(%s)\n" % name)
        self.fakegpslist[name].drain()
        self.remove(self.fakegpslist[name])
        self.daemon.remove_device(name)
        del self.fakegpslist[name]
    def client_add(self, commands):
        "Initiate a client session and force connection to a fake GPS."
        self.progress("gpsfake: client_add()\n")
        newclient = gps.gps(port=self.port, verbose=self.verbose)
        self.append(newclient)
        newclient.id = self.client_id + 1
        self.client_id += 1
        self.progress("gpsfake: client %d has %s\n" % (self.client_id,newclient.device))
        if commands:
            self.initialize(newclient, commands)
        return self.client_id
    def client_remove(self, cid):
        "Terminate a client session."
        self.progress("gpsfake: client_remove(%d)\n" % cid)
        for obj in self.runqueue:
            if isinstance(obj, gps.gps) and obj.id == cid:
                self.remove(obj)
                return True
        return False
    def wait(self, seconds):
        "Wait, doing nothing."
        self.progress("gpsfake: wait(%d)\n" % seconds)
        time.sleep(seconds)
    def gather(self, seconds):
        "Wait, doing nothing but watching for sentences."
        self.progress("gpsfake: gather(%d)\n" % seconds)
        #mark = time.time()
        time.sleep(seconds)
    def cleanup(self):
        "We're done, kill the daemon."
        self.progress("gpsfake: cleanup()\n")
        if self.daemon:
            self.daemon.kill()
            self.daemon = None
    def processingcommand(self):
        command = self.queue.get()
        if "stop" in command:
            print "Stop track cycle"
            self.stop = True
        elif "go" in command:
            self.stop = False
            print "Go track again"
        elif "file" in command:
            print "New track processing..."
            path = command.split(",")
            newtestload = TestLoad(path[1], predump=self.predump, slow=self.slow)

            for obj in self.runqueue:
                obj.testload.sentences = newtestload.sentences
                obj.index = 0
        elif "manual-begin" in command:
            self.manual = True
            self.stop = True

        elif "manual-end" in command:
            self.manual = False
        elif self.manual and "GPGGA" in command and "GPRMC" in command:
            if self.stop:
                for obj in self.runqueue:
                    obj.testload.sentences = []
                    obj.index = 0
                self.stop = False
            for obj in self.runqueue:
                obj.testload.sentences.append(command)
                obj.index = len(obj.testload.sentences) - 1



    def run(self, withoutfile):
        "Run the tests."
        try:
            self.progress("gpsfake: test loop begins\n")
            commandarrived = 0
            while self.daemon:
                # We have to read anything that gpsd might have tried
                # to send to the GPS here -- under OpenBSD the
                # TIOCDRAIN will hang, otherwise.

                if not self.queue.empty():
                    #print "Queue not empty"
                    self.processingcommand()
                    commandarrived = commandarrived + 1
                    if commandarrived > 1:
                        withoutfile = False

                if not withoutfile:
                    for device in self.runqueue:
                        if isinstance(device, FakeGPS):
                            device.read()
                    had_output = False
                    chosen = self.choose()

                    if isinstance(chosen, FakeGPS):
                        if chosen.exhausted and (time.time() - chosen.exhausted > TEST_TIMEOUT) and chosen.byname in self.fakegpslist:
                            sys.stderr.write("Test timed out: increase WRITE_PAD = %s\n" % WRITE_PAD)
                            raise SystemExit, 1
                        elif not chosen.go_predicate(chosen.index, chosen):
                            if chosen.exhausted == 0:
                                chosen.exhausted = time.time()
                                self.progress("gpsfake: GPS %s ran out of input\n" % chosen.byname)
                        else:
                            chosen.feed(self.stop, self.manual)
                    elif isinstance(chosen, gps.gps):
                        if chosen.enqueued:
                            chosen.send(chosen.enqueued)
                            chosen.enqueued = ""
                        while chosen.waiting():
                            chosen.read()
                            if chosen.valid & gps.PACKET_SET:
                                self.reporter(chosen.response)
                                if chosen.data["class"] == "DEVICE" and chosen.data["activated"] == 0 and chosen.data["path"] in self.fakegpslist:
                                    self.gps_remove(chosen.data["path"])
                                    self.progress("gpsfake: GPS %s removed (notification)\n" % chosen.data["path"])
                            had_output = True
                    else:
                        raise TestSessionError("test object of unknown type")
                    if not self.writers and not had_output:
                        self.progress("gpsfake: no writers and no output\n")
                        break
            self.progress("gpsfake: test loop ends\n")
        finally:
            self.cleanup()






    # All knowledge about locks and threading is below this line,
    # except for the bare fact that self.threadlock is set to None
    # in the class init method.

    def append(self, obj):
        "Add a producer or consumer to the object list."
        if self.threadlock:
            self.threadlock.acquire()
        self.runqueue.append(obj)
        if isinstance(obj, FakeGPS):
            self.writers += 1
        elif isinstance(obj, gps.gps):
            self.readers += 1
        if self.threadlock:
            self.threadlock.release()
    def remove(self, obj):
        "Remove a producer or consumer from the object list."
        if self.threadlock:
            self.threadlock.acquire()
        self.runqueue.remove(obj)
        if isinstance(obj, FakeGPS):
            self.writers -= 1
        elif isinstance(obj, gps.gps):
            self.readers -= 1
        self.index = min(len(self.runqueue)-1, self.index)
        if self.threadlock:
            self.threadlock.release()
    def choose(self):
        "Atomically get the next object scheduled to do something."
        if self.threadlock:
            self.threadlock.acquire()
        chosen = self.index
        self.index += 1
        self.index %= len(self.runqueue)
        if self.threadlock:
            self.threadlock.release()
        return self.runqueue[chosen]
    def initialize(self, client, commands):
        "Arrange for client to ship specified commands when it goes active."
        client.enqueued = ""
        if not self.threadlock:
            client.send(commands)
        else:
            client.enqueued = commands
    def start(self):
        self.threadlock = threading.Lock()
        threading.Thread(target=self.run)

# End
