import math, os, termios, time, socket, select, struct, collections

class Channel(object):
    BW_400 = 1 << 5
    BW_340 = 2 << 5
    BW_270 = 3 << 5
    BW_200 = 4 << 5
    BW_134 = 5 << 5
    BW_67  = 6 << 5

    def __init__(self, freq, dev, bw):
        self._freq = freq
        self._dev = dev
        self._bw = bw

    def __str__(self):
        return 'Channel(frequency = %fMHz, deviation = %dkHz, bandwidth = %dkHz)' % (
            self.frequency, self.deviation, self.bandwidth)

    @property
    def frequency(self):
        return 20. * (43. + self._freq / 4000)
    @property
    def deviation(self):
        return 15 * (1 + self._dev)
    @property
    def bandwidth(self):
        x = self._bw >> 5
        if 1 == x: return 400
        if 2 == x: return 340
        if 3 == x: return 270
        if 4 == x: return 200
        if 5 == x: return 134
        if 6 == x: return  67
        raise ValueError('invalid self._bw value')

RxFrame = collections.namedtuple('RxByte', ('bytes', 'rssi', 'timing'))

class Token(object):
    ESCAPE = '\x5a'

    @classmethod
    def extract(cls, buf):
        data = struct.unpack(cls.FIELDFMT, buf[2:2+cls.TAIL])
        return cls(**dict(zip(cls.FIELDNAMES, data)))

    def __init__(self, **kwargs):
        for k,v in kwargs.iteritems():
            if k not in self.FIELDNAMES:
                raise TypeError('Unexpected keyword argument %s for token %s' % (k, type(self).__name__))
            setattr(self, k, v)

    def serialize(self):
        return self.__str__()

    def __str__(self):
        args = [ getattr(self, k) for k in self.FIELDNAMES ]
        data = struct.pack(self.FIELDFMT, *args)
        return self.ESCAPE + self.TAG + data

    def __repr__(self):
        return type(self).__name__ + \
            '(' + ', '.join(['%s = %r' % (k,getattr(self, k)) for k in self.FIELDNAMES ]) + ')'

class PlainByteToken(object):
    TAG = None
    FIELDFMT = ''
    FIELDNAMES = ()
    TAIL = 0
    LENGTH = 1

    @classmethod
    def extract(cls, buf):
        return PlainByteToken(buf[0])

    def __init__(self, byte):
        self.byte = byte
    
    def __repr__(self):
        return 'PlainByteToken(0x%02x)' % ord(self.byte)

    def serialize(self):
        return self.byte

def make_token(name, tag, fieldfmt='', fieldnames=()):
    class ret(Token):
        TAG = tag
        FIELDFMT = fieldfmt
        FIELDNAMES = fieldnames
        TAIL = (0 if fieldfmt == '' else struct.calcsize(fieldfmt))
        LENGTH = 2+TAIL
    ret.__name__ = name
    return ret

UnescapeToken = make_token('UnescapeToken', '\xa5')
ResetRqToken = make_token('ResetRqToken', '\x01')
ResetAckToken = make_token('ResetAckToken', '\x5a', '<BBH', ('version_high', 'version_low', 'uniq_id'))
SetRxKnobsToken = make_token('SetRxKnobsToken', '\x02', '<HBB', ('frequency', 'deviation', 'rx_knobs'))
SetPowerToken = make_token('SetPowerToken', '\x03', '<B', ('power',))
SetBitrateToken = make_token('SetBitrateToken', '\x04', '<B', ('bitrate',))
TimingToken = make_token('TimingToken', '\x05', '<HI', ('timing_lo','timing_hi'))
PingToken = make_token('PingToken', '\x06', '<B', ('seq', ))
TxToken = make_token('TxToken', '\x07')
RxToken = make_token('RxToken', '\x08', '<HIH', ('timing_lo', 'timing_hi', 'rssi'))

SetPosToken = make_token('SetPosToken', '\x09', '<ii', ('x', 'y')) # used to set the simulated position of the devices

allTokens = (
    UnescapeToken, ResetRqToken, ResetAckToken,
    SetRxKnobsToken, SetPowerToken, SetBitrateToken,
    TimingToken, PingToken, TxToken, RxToken, 
    SetPosToken
)

def extract_token(buf):
    if 0 == len(buf):
        return None

    if buf[0] != Token.ESCAPE:
        return PlainByteToken(buf[0])

    if len(buf) < 2:
        return None
    tag = buf[1]
    for t in allTokens:
        if tag == t.TAG:
            if len(buf) < t.LENGTH:
                return None
            return t.extract(buf)

    raise IOError('unrecognized token (tag 0x%02x)' % ord(tag))

class PingFuture(object):
    def __init__(self, nic, seq):
        self.nic = nic
        self.seq = seq
        self.acked = False
        self.callbacks = []

    def await(self, deadline=None):
        while not self.acked and self.nic._rx(deadline) is not None:
            pass
        return self.acked

    def add_callback(self, cb):
        self.callbacks.append(cb)

def timing2us(timing): return timing * 0.54253472
def us2timing(s): return int(math.ceil(s*1.8432))

class NIC(object):
    RSSI_103 = 0
    RSSI_97  = 1
    RSSI_91  = 2
    RSSI_85  = 3
    RSSI_79  = 4
    RSSI_73  = 5

    GAIN_0   = 0 << 3
    GAIN_6   = 1 << 3
    GAIN_14  = 2 << 3
    GAIN_20  = 3 << 3

    POWER_0   = 0
    POWER_25  = 1
    POWER_50  = 2
    POWER_75  = 3
    POWER_100 = 4
    POWER_125 = 5
    POWER_150 = 6
    POWER_175 = 7

    BITRATE_600    = 0x80 |  71 #   598.659 bps
    BITRATE_1200   = 0x80 |  35 #  1197.318 bps
    BITRATE_2400   =        143 #  2394.636 bps
    BITRATE_3600   =         95 #  3591.954 bps
    BITRATE_4800   =         71 #  4789.272 bps
    BITRATE_9600   =         35 #  9578.544 bps
    BITRATE_11400  =         29 # 11494.253 bps
    BITRATE_19200  =         17 # 19157.088 bps
    BITRATE_28800  =         11 # 28735.632 bps
    BITRATE_38400  =          8 # 38314.176 bps, too fast
    BITRATE_57600  =          5 # 57471.264 bps, too fast
    BITRATE_115200 =          2 #114942.534 bps, too fast

    # frequency 868.32 MHz, deviation 60kHz, bandwidth 67kHz
    defaultChannel = Channel(freq = 0x680, dev = 3, bw = Channel.BW_67)
    defaultRSSI = RSSI_91
    defaultGain = GAIN_20
    defaultPower = POWER_175
    defaultBitrate = BITRATE_9600

    def __init__(self, comm, deadline=None):
        self._comm = comm
        self._pingseq = 0
        self.reset(deadline)

    def get_uniq_id(self):
        return self._uniq_id

    def get_approx_timing(self):
        return int(math.ceil(1e+6 * (time.time() - self._t0)))

    def reset(self, deadline=None):
        self._pings = dict()
        self._rxbuf = ''

        self._comm.send(ResetRqToken().serialize())

        while True:
            e = self._nextToken(deadline)
            if e is None:
                raise IOError('failed to reset NIC in given time')
            if isinstance(e, ResetAckToken):
                break

        self._txqueuelen = 0
        self._txping = None

        self._rxqueue = ''
        self._rxframes = []

        self._uniq_id = e.uniq_id
        self._t0 = time.time()

        self._channel = self.defaultChannel
        self._rssi = self.defaultRSSI
        self._gain = self.defaultGain
        self._power = self.defaultPower
        self._bitrate = self.defaultBitrate

        self._comm.send(SetRxKnobsToken(
            frequency = self._channel._freq,
            deviation = self._channel._dev,
            rx_knobs = self._channel._bw | self._gain | self._rssi
        ).serialize())
        self._comm.send(SetBitrateToken(
            bitrate = self._bitrate
        ).serialize())
        self._comm.send(SetPowerToken(
            power = self._power
        ).serialize())

        if not self.sync(deadline):
            raise IOError('failed to set up radio parameters in given time')

    def ping(self):
        seq = self._pingseq = (self._pingseq + 1) & 255
        if seq in self._pings:
            raise IOError('pings overflow! (%d)' % seq)
        self._pings[seq] = future = PingFuture(self, seq)
        self._comm.send(PingToken(seq=seq).serialize())
        return future

    def sync(self, deadline=None):
        return self.ping().await(deadline)

    def timing(self, timing):
        timing = us2timing(timing)
        self._comm.send(TimingToken(
            timing_lo = timing & (1<<16)-1,
            timing_hi = timing >> 16
        ).serialize())

    def set_channel(self, channel):
        self._comm.send(SetRxKnobsToken(
            frequency = channel._freq,
            deviation = channel._dev,
            rx_knobs = channel._bw | self._gain | self._rssi
        ).serialize())
        self._channel = channel

    def set_bitrate(self, bitrate):
        self._comm.send(SetBitrateToken(
            bitrate = bitrate
        ).serialize())
        self._bitrate = bitrate

    def set_sensitivity(self, gain, rssi):
        self._comm.send(SetRxKnobsToken(
            frequency = self._channel._freq,
            deviation = self._channel._dev,
            rx_knobs = self._channel._bw | gain | rssi
        ).serialize())
        self._gain = gain
        self._rssi = rssi

    def set_power(self, power):
        self._comm.send(SetPowerToken(
            power = power
        ).serialize())
        self._power = power

    def set_pos(self, x, y):
        self._comm.send(SetPosToken(
            x = x, y = y
        ).serialize())

    def tx(self, payload, overrun_fail=True, deadline=None):
        if not payload:
            return

        nic_txbuf_size = 1536

        if len(payload) > nic_txbuf_size:
            raise IOError('packet is too large')

        # prevent pings overflow
        while self._rx(0) is not None:
            pass

        # if we still need to wait for some data to be transmitted
        # and we're allowed to do so, do it now
        if not overrun_fail:
            while self._txqueuelen + len(payload) > nic_txbuf_size and \
                  self._rx(deadline) is not None:
                pass

        # fail if no room for payload
        if self._txqueuelen + len(payload) > nic_txbuf_size:
            raise IOError('tx buffer overrun')

        self._comm.send(
            payload.replace(Token.ESCAPE, Token.ESCAPE + UnescapeToken.TAG) +
            TxToken().serialize()
        )

        self._txqueuelen += len(payload)

        def ping_cb():
            self._txqueuelen -= len(payload)
        ping = self.ping()
        ping.add_callback(ping_cb)
        return ping

    def _nextToken(self, deadline = None):
        while True:
            e = extract_token(self._rxbuf)
            if e is not None:
                self._rxbuf = self._rxbuf[e.LENGTH:]
                return e

            rx = self._comm.recv(deadline)
            if not rx:
                return None
            self._rxbuf += rx

    def _rx(self, deadline = None):
        e = self._nextToken(deadline)
        if e is None:
            pass
        elif isinstance(e, UnescapeToken):
            self._rxqueue += Token.ESCAPE
        elif isinstance(e, PlainByteToken):
            self._rxqueue += e.byte
        elif isinstance(e, RxToken):
            self._rxframes.append(RxFrame(
                bytes = self._rxqueue,
                rssi = e.rssi,
                timing = int(math.ceil(timing2us(e.timing_hi << 16 | e.timing_lo)))
            ))
            self._rxqueue = ''
        elif isinstance(e, PingToken):
            if e.seq in self._pings:
                ping = self._pings.pop(e.seq)
                ping.acked = True
                for cb in ping.callbacks:
                    cb()
        else:
            raise IOError('unexpected token received from NIC: %r' % e)

        return e

    def rx(self, deadline = None):
        while True:
            if len(self._rxframes) > 0:
                return self._rxframes.pop(0)

            e = self._rx(deadline)
            if e is None:
                return None

class NetComm(object):
    def __init__(self, host='themis.lo14.wroc.pl', port=2048):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self._sock.connect((host, port))
        self._poll = select.poll()
        self._poll.register(self._sock.fileno(), select.POLLIN)

    def recv(self, deadline = None):
        if deadline is None:
            timeout = None
        elif deadline == 0:
            timeout = 0
        else:
            timeout = max(0, deadline - time.time())
            timeout = int(math.ceil(1000. * timeout))

        if not self._poll.poll(timeout):
            return None
        rx = self._sock.recv(4096)
        if 0 == len(rx):
            raise IOError('lost comm')
        return rx

    def send(self, data):
        self._sock.send(data)

    def fileno(self):
        return self._sock.fileno()

class USBComm(object):
    def __init__(self, device=None):
        if device is None:
            device = USBComm.detect_device()
        self._fd = os.open(device, os.O_RDWR | os.O_NOCTTY)

        rawmode = [
            0, # iflags
            0, # oflags
            termios.CS8, # cflags
            0, # lflag
            termios.B230400, # ispeed
            termios.B230400, # ospeed
            [0,0,0,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0] # cc; vmin=1, vtime=0
        ]
        termios.tcsetattr(self._fd, termios.TCSADRAIN, rawmode)

        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN)

    @staticmethod
    def detect_device():
        sysfsdir = '/sys/bus/usb-serial/devices/'
        magic = 'FT232R USB UART'

        candidates = []
        for devname in os.listdir(sysfsdir):
            magicfile = os.path.realpath(sysfsdir + devname + '/../interface')
            if not os.path.isfile(magicfile):
                continue
            with open(magicfile, 'r') as f:
                if not f.read().startswith(magic):
                    continue
            candidates.append(devname)

        if not candidates:
            raise IOError('no iinic detected; you may pass device= argument if you know where the device is')
        if len(candidates) > 1:
            raise IOError('more than one possible iinic detected (' + ', '.join(candidates) + '); pass device= argument to select one')
        return '/dev/' + candidates[0]

    def recv(self, deadline = None):
        if deadline is None:
            timeout = None
        elif deadline == 0:
            timeout = 0
        else:
            timeout = max(0, deadline - time.time())
            timeout = int(math.ceil(1000. * timeout))

        if not self._poll.poll(timeout):
            return None
        rx = os.read(self._fd, 4096)
        #print 'recv: ' + ' '.join(['%02x' % ord(c) for c in rx])
        return rx

    def send(self, data):
        #print 'send: ' + ' '.join(['%02x' % ord(c) for c in data])
        os.write(self._fd, data)

    def fileno(self):
        return self._fd

