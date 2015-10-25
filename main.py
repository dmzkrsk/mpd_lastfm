# -*- coding: utf-8 -*-
#!/usr/bin/env python
import mpd
import socket
import logging
import sys
import time
import os
import re

APPNAME = 'MPD-Last.FM'
SLEEP_LARGE = 2
SLEEP_SMALL = .5
NOSCROBBLE = re.compile('^(https?|mms|rtsp)://.+', re.I)
LFMCLIENT_PORT = 33367

# Где искать конфиги

CONFIGFILES = [
    '/etc/mpdlastfm.conf',
    os.path.join( os.environ.get('HOME', ''), '.mpdlastfm.conf' )
]

# Конфиг по умолчанию

config = {
    # параметры подключения к mpd
    'mpd_host' : 'localhost',
    'mpd_port' : 6600,
    'mpd_pass' : '',
    # путь до корня с музыкой. Клиент Last.FM не прочь получить полный путь
    'mpd_root' : os.path.join( os.environ['HOME'], '.music' ),
    # файл с логом. ротации нет
    'log_file' : '/var/log/mpdlfm/mdlfm.log',
}

# Простая читалка конфигов

for cfile in CONFIGFILES:
    try:
        for line in file(cfile).xreadlines():
            line.strip()
            if line[0] == '#': continue
            try:
                key, value = [x.strip() for x in line.split('=')]
                if key in config:
                    config[key] = value
            except ValueError:
                continue
    except:
        pass

config['mpd_port'] = int( config['mpd_port'] )

# Настраиваем лог

log_dir = os.path.dirname( config['log_file'] )
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%d/%m/%Y %H:%M:%S',
                    filename=config['log_file'],
                    filemode='w')

console = logging.StreamHandler()
logging.getLogger('').addHandler(console)
formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
console.setFormatter(formatter)
logger = logging.getLogger(APPNAME)

logger.info(u'Начинаем')

class LFMClient():
    def __init__(self, host = 'localhost',
            port = LFMCLIENT_PORT, timeout = 5.0):
        self.id = "mdc" # Это ID от mpdscribble
        self.host = host
        self.port = port
        self.timeout = timeout

    def _build_command(self, command, **kwargs):
        kwargs['c'] = self.id

        # ключ=значение
        # объединяем при помощи &
        # & в значениях экранируем им же: &&
        command += ' '
        for key, value in kwargs.items():
            command += '%s=%s&' % (key, value.replace('&', '&&'))

        # не забываем про перевод строки
        command = command[:-1] + '\n'

        return command

    def send_command(self, command, **kwargs):
        command = self._build_command(command, **kwargs)

        try:
            # Клиент закрывает соединение после каждой (?) комманды
            # Так что не надо заморачиваться с постоянным соединением
            _socket = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
            _socket.connect(( self.host, self.port ))
            _socket.settimeout( self.timeout )

            command_bytes = command.encode('utf-8')
            sent = _socket.send( command_bytes )

            if sent == len( command_bytes ):
                logger.debug(u"Отправлено: %s" % command.strip())
            else:
                logger.error( u"Не удалось отправить комманду клиенту" )

            _socket.shutdown(socket.SHUT_RDWR)
            _socket.close()
        except socket.error, e:
            logger.error('Нет соединения с Last.FM клиентом: %s' % e[1] )

    def track_changed(self, trackinfo):
        logger.info(u'Начинается воспроизведение нового трека)')

        try:
            # Надеюсь, что MPD всегда отдаёт инфу в UTF-8
            artist      = trackinfo.get('artist', '').decode('utf-8')
            title       = trackinfo.get('title', '').decode('utf-8')
            filename    = trackinfo.get('file', '').decode('utf-8')
            album       = trackinfo.get('album', '').decode('utf-8')
        except UnicodeDecodeError, e:
            logger.error(u'Неверная последовательность символов в значении тега: %s'
                % e)
            self.send_command("STOP")
            return

        length = int( trackinfo.get('time', 0) )

        if artist and title and length:
            logger.info(u'Трек: %s - %s (%s)' % (artist, title, filename))
        else:
            # Пишем предупреждение в лог, если надо
            # Но клиенту всё равно отправим потом
            # Пусть сам решает, что делать с коротким или с неправильными
            # тегами файлом
            logger.warn(u'В файле отсутствуют теги или он слишком короткий. Скорее всего он не будет заскробблен')

        mbId = ''

        self.send_command("START", a = artist, t = title, b = album, m = mbId,
                l = str(length), p = filename)

    def state_changed(self, oldstate, newstate):
        if newstate == 'play':
            logger.info(u'Воспроизведение возобновлено')
            self.send_command('RESUME')
            pass
        elif newstate == 'stop':
            logger.info(u'Воспроизведение остановлено')
            self.send_command('STOP')
            pass
        elif newstate == 'pause':
            logger.info(u'Воспроизведение поставлено на паузу')
            self.send_command('PAUSE')
            pass
        else:
            logger.warn(u'MPD находится в неизвестном состоянии: %s'
                % newstate)

class MPDHelper():
    def __init__(self, host, port, password,
            root, state_changed, track_changed):
        self._mpd = mpd.MPDClient()

        self.host = host
        self.port = port
        self.password = password
        self.root = root

        self.state_changed = state_changed
        self.track_changed = track_changed

        self._servername = '%s:%d' % (host, port)
        if password:
            self._servername = '******@' + self._servername

        self.connection_status = None
        self.last_file_played = None
        self.last_state = None

    def _connect(self):
        # Пингуем, коннектимся, реконнектимся
        # «Умное» постоянное соединение
        try:
            self._mpd.ping()
        except socket.error:
            pass
        except mpd.ConnectionError:
            try:
                self._mpd.disconnect()
            except socket.error:
                pass
            except mpd.ConnectionError:
                pass

            try:
                self._mpd.connect(self.host, self.port)
                self._mpd.ping()

                if self.password:
                    self._mpd.password(self.password)

                if self.connection_status is None:
                    logger.info(u"Установленно соединение с MPD (%s)"
                        % (self._servername,))
                else:
                    logger.warn(u"Переподключение к MPD (%s)"
                        % (self._servername,))

                self.connection_status = True
            except (mpd.ConnectionError, mpd.CommandError, socket.error), e:
                try:
                    # На всякий случай отключаемся
                    self._mpd.disconnect()
                except socket.error:
                    pass
                except mpd.ConnectionError:
                    pass

                error_message = e[1] if isinstance(e, socket.error) else e

                if self.connection_status:
                    logger.error(u"Потеряно подключение к MPD (%s): %s"
                        % (self._servername, error_message))
                else:
                    logger.error(u"Не удается подключиться к MPD (%s): %s. Проверьте адрес сервера и пароль в настройках"
                        % (self._servername, error_message))

                self.connection_status = False

        return self.connection_status

    def poll(self):
        try:
            currentsong, status = self._mpd.currentsong(), self._mpd.status()

            # Пытаемся получить полное имя файла на диске, если возможно
            file = currentsong.get('file')
            if self.root and file:
                if not NOSCROBBLE.match(file):
                    if file.startswith('file://'):
                        file = file[7:]
                    fullpath = os.path.join(self.root, file)
                    if os.path.exists(fullpath):
                        currentsong['file'] = os.path.join(self.root, file)

        except (mpd.ConnectionError, mpd.CommandError, socket.error), e:
            error_message = e[1] if isinstance(e, socket.error) else e
            logger.warn(u'Невозможно получить информацию о текущей композиции: %s'
                % error_message)
            return False

        new_state = status.get('state')

        # Если изменилось состояние, то сообщаем клиенту об этом
        # Надо учесть, что при запуске скрипта MPD уже может работать, но при
        # этом стоять на паузе и пр. Об этом сообщать не будем

        if new_state != self.last_state:
            if self.state_changed and self.last_state is not None:
                self.state_changed(self.last_state, new_state)

        # Если изменилось имя файла
        # Если мы вышли из состояния ОСТАНОВЛЕННО
        # ... и при этом сейчас находимся в состоянии ВОСПРОИЗВЕДЕНИЕ
        # То сообщаем о новом треке
        if (
                (
                    currentsong.get('file') != self.last_file_played or
                    self.last_state == 'stop'
                ) and
                new_state == 'play'
            ):
            self.track_changed( currentsong )

        # Сохраняем состояние и имя проигрываемого файла
        self.last_file_played = currentsong.get('file')
        self.last_state = new_state

        return True

lfm = LFMClient()
mpdpoll = MPDHelper(config['mpd_host'], config['mpd_port'], config['mpd_pass'], config['mpd_root'], lfm.state_changed, lfm.track_changed)

# Пробуем запустить Last.FM клиента сами
# Если он уже работает, то вторая копия не запустится
# Так что дополнительно проверять не нужно
from subprocess import Popen
for procname in ['last.fm', 'lastfm']:
    try:
        logger.debug(u'Попытка запустить процесс %s' % procname)
        stdin = open(os.devnull, 'r')
        stdout = open(os.devnull, 'a+')
        stderr = open(os.devnull, 'a+')

        Popen([procname, "-tray"], stdin = stdin, stdout = stdout, stderr = stderr).pid
        logger.debug(u'Процесс запущен. Немного подождем')
        time.sleep(3)
        break
    except:
        logger.debug(u'Не удалось запустить процесс: %s' % sys.exc_info()[1])

# Пока живы опрашиваем MPD
while True:
    # «Умный» connect
    mpdpoll._connect()

    # Если нет соединения с MPD, то выдержим паузу побольше
    if not mpdpoll.connection_status:
        time.sleep(SLEEP_LARGE)
        continue

    # Если клиент отвечает, то опрашиваем
    mpdpoll.poll()
    # и выдерживаем короткую паузу, перед следущей попыткой
    time.sleep(SLEEP_SMALL)
Google AJAX Search API
