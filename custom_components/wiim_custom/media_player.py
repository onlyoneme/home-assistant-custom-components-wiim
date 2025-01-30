"""
Support for WiiM devices.

For more details about this platform, please refer to the documentation at
https://github.com/onlyoneme/home-assistant-custom-components-wiim
"""

import asyncio
import async_timeout
import voluptuous as vol

from datetime import timedelta
import logging

import string
import aiohttp
from http import HTTPStatus
from aiohttp.client_exceptions import ClientError

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.aiohttp import AiohttpRequester
from lxml import etree as ET

from homeassistant.util import Throttle
from homeassistant.util.dt import utcnow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA, 
    MediaPlayerEntity,
    MediaPlayerDeviceClass,
    MediaPlayerEntityFeature,	
)

from homeassistant.components import media_source
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)

from homeassistant.components.media_player.const import (
    MediaType,
    RepeatMode
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_DEVICE_CLASS,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    STATE_BUFFERING,
)


from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

ICON_DEFAULT = 'mdi:speaker'
ICON_PLAYING = 'mdi:speaker-wireless'
ICON_MUTED = 'mdi:speaker-off'
ICON_BLUETOOTH = 'mdi:speaker-bluetooth'
ICON_PUSHSTREAM = 'mdi:cast-audio'

ATTR_FWVER = 'firmware'
ATTR_DEVMODEL = 'device_model'
ATTR_PL_TRACKS = 'pl_tracks'
ATTR_PL_TRCRT = 'pl_track_current'
ATTR_TRCRT = 'track_current'
ATTR_STURI = 'stream_uri'
ATTR_UUID = 'uuid'
ATTR_DEBUG = 'debug_info'
ATTR_BITRATE = 'bit_rate'
ATTR_SAMPLERATE = 'sample_rate'
ATTR_DEPTH = 'bit_depth'
ATTR_FIXED_VOL = 'fixed_vol'
ATTR_SLAVE = 'slave'
ATTR_MASTER_UUID = 'master_uuid'
ATTR_ART_URL = 'art_url'

CONF_NAME = 'name'
CONF_VOLUME_STEP = 'volume_step'
CONF_UUID = 'uuid'

DEFAULT_VOLUME_STEP = 5


DEBUGSTR_ATTR = True
MAX_VOL = 100

UPNP_TIMEOUT = 2
API_TIMEOUT = 2

UNA_THROTTLE = timedelta(seconds=20)
CONNECT_PAUSED_TIMEOUT = timedelta(seconds=300)
AUTOIDLE_STATE_TIMEOUT = timedelta(seconds=1)

MODEL_MAP = {'Muzo_Mini': 'WiiM Mini',
             'WiiM_Pro_with_gc4a': 'WiiM Pro',
             'WiiM_Pro_Plus': 'WiiM Pro Plus',
             'WiiM_AMP': 'WiiM Amp'}

SOURCES = {'line-in': 'Analog', 
           'optical': 'Toslink',
           'HDMI': 'HDMI'}

SOURCES_MAP = {'-1': 'Idle', 
               '0': 'Idle', 
               '1': 'Airplay', 
               '2': 'DLNA',
               '3': 'Amazon',
			   '4': '???',
               '5': 'Chromecast',
               '10': 'Network',
               '20': 'Network',			   
               '31': 'Spotify',
               '32': 'TIDAL',
			   '33': 'Roon',
			   '34': 'Squeezelite',
               '40': 'Analog',
               '41': 'Bluetooth',
               '43': 'Toslink',	
               '49': 'HDMI',			   
               '99': 'Idle'}

SOURCES_IDLE = ['-1', '0', '99']
SOURCES_LIVEIN = ['40', '41', '43', '49']
SOURCES_STREAM = ['1', '2', '3', '4', '5', '10', '20', '33', '34']
SOURCES_CONNECT = ['31', '32']

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_UUID, default=''): cv.string,
        vol.Optional(CONF_VOLUME_STEP, default=DEFAULT_VOLUME_STEP): vol.All(int, vol.Range(min=1, max=25)),
    }
)




class WiiMData:
    """Storage class for platform global data."""
    def __init__(self):
        """Initialize the data."""
        self.entities = []

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the WiiM platform."""

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = WiiMData()

    name = config.get(CONF_NAME)
    host = config.get(CONF_HOST)
    uuid = config.get(CONF_UUID)
    volume_step = config.get(CONF_VOLUME_STEP)


    state = STATE_IDLE

    initurl = "https://{0}/httpapi.asp?command=getStatusEx".format(host)
    
    try:
        websession = async_get_clientsession(hass)
        response = await websession.get(initurl, ssl=False)

        if response.status == HTTPStatus.OK:
            data = await response.json(content_type=None)
            _LOGGER.debug("HOST: %s DATA response: %s", host, data)

            try:
                uuid = data['uuid']
            except KeyError:
                pass

            if name == None:
                try:
                    name = data['DeviceName']
                except KeyError:
                    pass

        else:
            _LOGGER.warning(
                "Get Status UUID failed, response code: %s Full message: %s",
                response.status,
                response,
            )
            state = STATE_UNAVAILABLE

    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        _LOGGER.warning(
            "Failed communicating with WiiM (start) '%s': uuid: %s %s", host, uuid, type(error)
        )
        state = STATE_UNAVAILABLE

    wiim = WiiMDevice(name, 
                      host, 
                      volume_step,
                      uuid,
                      state,
                      hass)

    async_add_entities([wiim])
		
class WiiMDevice(MediaPlayerEntity):
    """WiiM Player Object."""

    def __init__(self, 
                 name, 
                 host, 
                 volume_step,
                 uuid,
                 state,
                 hass):
        """Initialize the media player."""
        self._uuid = uuid
        self._fw_ver = '1.0.0'
        self._device_model = 'Unknown'
        requester = AiohttpRequester(UPNP_TIMEOUT)
        self._factory = UpnpFactory(requester, disable_unknown_out_argument_error=True)
        self._upnp_device = None
        self._service_transport = None
        self._service_control = None
        self._features = None
        self._preset_key = 6
        self._name = name
        self._host = host
        self._icon = ICON_DEFAULT
        self._state = state
        self._volume = 0
        self._volume_step = volume_step
        self._source = None
        self._prev_source = None
        self._source_list = SOURCES.copy()

        self._muted = False
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = None
        self._connect_paused_at = None
        self._idletime_updated_at = None
        self._shuffle = False
        self._repeat = RepeatMode.OFF
        self._media_album = None
        self._media_artist = None
        self._media_prev_artist = None
        self._media_title = None
        self._media_prev_title = None
        self._media_image_url = None
        self._media_uri = None
        self._media_uri_final = None
        self._media_source_uri = None
        self._player_statdata = {}
        self._player_mediainfo = {}
        self._player_deviceinfo = {}
        self._first_update = True

        self._pl_tracks = None
        self._pl_trackc = None
        self._trackc = None

        self._playing_stream = False
        self._playing_liveinput = False
        self._playing_connect = False
        self._playing_mediabrowser = False
        self._wait_for_mcu = 0
        self._unav_throttle = False
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None
        self._fixed_volume = None
		
        self._slave = None
        self._master_uuid = None

    async def async_added_to_hass(self):
        """Record entity."""
        self.hass.data[DOMAIN].entities.append(self)

		
    async def call_wiim_httpapi(self, cmd, jsn):
        """Get the latest data from HTTPAPI service."""
        _LOGGER.debug("For: %s  cmd: %s  jsn: %s", self._name, cmd, jsn)
        url = "https://{0}/httpapi.asp?command={1}".format(self._host, cmd)
        
        if self._first_update:
            timeout = 10
        else:
            timeout = API_TIMEOUT
        
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(timeout):
                response = await websession.get(url, ssl=False)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "Failed communicating with WiiM (httpapi) '%s': %s", self._name, type(error)
            )
            return False

        if response.status == HTTPStatus.OK:
            if jsn:
                data = await response.json(content_type=None)
            else:
                data = await response.text()
                _LOGGER.debug("For: %s  cmd: %s  resp: %s", self._name, cmd, data)
        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )
            return False

        return data

		
    @Throttle(UNA_THROTTLE)
    async def async_get_status(self):
        resp1 = False
        resp2 = False
        resp3 = False
        if self._upnp_device is not None:
            if self._service_transport is None:
                self._service_transport = self._upnp_device.service('urn:schemas-upnp-org:service:AVTransport:1')
            if self._service_control is None:
                self._service_control = self._upnp_device.service('urn:schemas-upnp-org:service:RenderingControl:1')

            resp1 = dict()
            resp2 = dict()
            resp3 = dict()
            try:
                resp1 = await self._service_transport.action("GetInfoEx").async_call(InstanceID=0)
                _LOGGER.debug("GetInfoEx for: %s, UPNP data: %s", self.entity_id, resp1)
                resp2 = await self._service_control.action("GetControlDeviceInfo").async_call(InstanceID=0)
                #_LOGGER.debug("GetControlDeviceInfo for: %s, UPNP data: %s", self.entity_id, resp2)
                resp3 = await self._service_transport.action("GetMediaInfo").async_call(InstanceID=0)
                #_LOGGER.debug("GetMediaInfo for: %s, UPNP data: %s", self.entity_id, resp3)
            except:
                resp1 = False
                resp2 = False
                resp3 = False
        if resp1 is False or resp2 is False or resp3 is False:
            _LOGGER.debug('Unable to connect to device via UPnP: %s, %s', self.entity_id, self._name)
            self._state = STATE_UNAVAILABLE
            self._unav_throttle = True
            self._wait_for_mcu = 0
            self._playhead_position = None
            self._duration = None
            self._position_updated_at = None
            self._media_title = None
            self._media_artist = None
            self._media_album = None
            self._media_image_url = None
            self._media_uri = None
            self._media_uri_final = None
            self._media_source_uri = None
            self._trackc = None
            self._pl_tracks = None
            self._pl_trackc = None
            self._playing_mediabrowser = False
            self._playing_stream = False
            self._playing_liveinput = False
            self._playing_connect = False
            self._source = None
            self._upnp_device = None
            self._first_update = True
            self._player_statdata = None
            self._player_mediainfo = None
            self._player_deviceinfo = None
            self._service_transport = None
            self._service_control = None
            self._icon = ICON_DEFAULT
            self._samplerate = None
            self._bitrate = None
            self._bitdepth = None
            self._features = None	
            self._slave = None
            self._master_uuid = None			
            return
        self._player_statdata = resp1.copy()
        self._player_deviceinfo = resp2.copy()
        self._player_mediainfo = resp3.copy()
		
    async def async_trigger_schedule_update(self, before):
        await self.async_schedule_update_ha_state(before)	


    async def async_update(self):
        """Update state."""
        #_LOGGER.debug("01 Start update %s, %s", self.entity_id, self._name)

        if self._upnp_device is None: 
            url = "http://{0}:49152/description.xml".format(self._host)
            try:
                self._upnp_device = await self._factory.async_create_device(url)
            except:
                _LOGGER.warning(
                    "Failed communicating with WiiM (UPnP) '%s' at %s", self._name, self._host
                )

        if self._unav_throttle:
            await self.async_get_status()
        else:
            await self.async_get_status(no_throttle=True)

        if self._player_statdata is None:
            _LOGGER.debug("First update/No response from api: %s, %s", self.entity_id, self._player_statdata)
            return

        if isinstance(self._player_statdata, dict):
            self._unav_throttle = False
            if self._first_update or (self._state == STATE_UNAVAILABLE):
                #_LOGGER.debug("03 Update first time getStatusEx %s, %s", self.entity_id, self._name)
                device_status = await self.call_wiim_httpapi("getStatusEx", True)
                if device_status is not None:
                    if isinstance(device_status, dict):
                        if self._state == STATE_UNAVAILABLE:
                            self._state = STATE_IDLE
                        
                        try:
                            self._uuid = device_status['uuid']
                        except KeyError:
                            pass

                        try:
                            self._name = device_status['DeviceName']
                        except KeyError:
                            pass

                        try:
                            self._fw_ver = device_status['firmware']
                        except KeyError:
                            self._fw_ver = '1.0.0'

                        try:
                            self._device_model = MODEL_MAP.get(device_status['project'], 'Unknown')
                        except KeyError:
                            self._device_model = 'Unknown'

                        try:
                            self._fixed_volume = device_status['volume_control']
                        except KeyError:
                            pass							

                        try:
                            self._preset_key = int(device_status['preset_key'])
                        except KeyError:
                            self._preset_key = 6



                        if self._first_update:
                            self._duration = 0
                            self._playhead_position = 0
                            self._idletime_updated_at = utcnow()
                            self._first_update = False

            self._position_updated_at = utcnow()

            self._pl_tracks = self._player_mediainfo['NrTracks']
            self._pl_trackc = self._player_statdata['Track']
            self._slave = self._player_statdata['SlaveFlag']
            self._master_uuid = self._player_statdata['MasterUUID']

            #_LOGGER.debug("04 Update VOL, Shuffle, Repeat, STATE %s, %s", self.entity_id, self._name)
            self._volume = self._player_statdata['CurrentVolume']
            self._muted = bool(self._player_deviceinfo['CurrentMute']) 

            self._shuffle = {
                2: True,
                3: True,
                5: True,
            }.get(self._player_statdata['LoopMode'], False)

            self._repeat = {
                0: RepeatMode.ALL,
                1: RepeatMode.ONE,
                2: RepeatMode.ALL,
                5: RepeatMode.ONE,
            }.get(self._player_statdata['LoopMode'], RepeatMode.OFF)

            
            if self._player_statdata['PlayType'] in SOURCES_IDLE or self._player_statdata['CurrentTransportState'] in ['STOPPED', 'NO_MEDIA_PRESENT']: 
                if utcnow() >= (self._idletime_updated_at + AUTOIDLE_STATE_TIMEOUT):
                    self._state = STATE_IDLE
                    #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['CurrentTransportState'] in ['PLAYING']:
                self._state = STATE_PLAYING
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['CurrentTransportState'] in ['PAUSED_PLAYBACK']:
                self._state = STATE_PAUSED
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['CurrentTransportState'] in ['TRANSITIONING']:
                self._state = STATE_BUFFERING
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)

            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                x = self._player_statdata['TrackDuration']
                self._duration = int(sum([int(x)*int(y) for x,y in zip([3600,60,1],x.split(":"))]))
                x = self._player_statdata['RelTime']
                self._playhead_position = int(sum([int(x)*int(y) for x,y in zip([3600,60,1],x.split(":"))]))
                #_LOGGER.debug("04 Update DUR, POS %s, %s, %s, %s, %s", self.entity_id, self._name, self._state, self._duration, self._playhead_position)
            else:
                self._duration = 0
                self._playhead_position = 0

            #_LOGGER.debug("05 Update self._playing_whatever %s, %s", self.entity_id, self._name)
            self._playing_connect = self._player_statdata['PlayType'] in SOURCES_CONNECT		
            self._playing_liveinput = self._player_statdata['PlayType'] in SOURCES_LIVEIN
            self._playing_stream = self._player_statdata['PlayType'] in SOURCES_STREAM

            if self._player_statdata['PlayType'] not in ['10', '20']:
                self._playing_mediabrowser = False


            self._source = SOURCES_MAP.get(self._player_statdata['PlayType'], 'Network')			


            if self._playing_liveinput:
                #_LOGGER.debug("08 Line Inputs: %s, %s", self.entity_id, self._name)
                if self._source == 'Idle':
                    self._state = STATE_IDLE
                    self._media_title = None
                else:
                    self._state = STATE_PLAYING
                    self._media_title = self._source

                self._media_artist = None
                self._media_album = None
                self._media_image_url = None
                self._connect_paused_at = None

            elif self._playing_connect:
                #_LOGGER.debug("09 it's playing Spotify/Tidal: %s, %s", self.entity_id, self._name)
                if self._state != STATE_IDLE:
                    await self.async_update_via_upnp()
                if self._state == STATE_PAUSED:
                    if self._connect_paused_at == None:
                        self._connect_paused_at = utcnow()
                else:
                    self._connect_paused_at = None

                if self._state == STATE_IDLE:
                    self._source = None

            elif self._playing_stream and not self._playing_mediabrowser:
                if self._state != STATE_IDLE:
                    await self.async_update_via_upnp()
                self._connect_paused_at = None
            else:
                #_LOGGER.debug("09 it's playing something else: %s, %s", self.entity_id, self._name)
                self._connect_paused_at = None
                self._media_artist = None
                self._media_album = None
                self._media_image_url = None
                if self._state not in [STATE_PLAYING, STATE_PAUSED]:
                    self._media_title = None
                else:
                    self._media_title = self._source

            self._media_prev_artist = self._media_artist
            self._media_prev_title = self._media_title

            #    if self._connect_paused_at != None:
            #        if utcnow() >= (self._connect_paused_at + CONNECT_PAUSED_TIMEOUT):
            #            # Prevent sticking in Pause mode for a long time (Spotify doesn't have a stop button on the app)
            #            await self.async_media_stop()
            #            return

        else:
            _LOGGER.error("Erroneous JSON during update and process self._player_statdata: %s, %s", self.entity_id, self._name)



        return True


    @property
    def name(self):
        """Return the name of the device."""

        return self._name		
	

    @property
    def icon(self):
        """Return the icon of the device."""


        if self._state in [STATE_PAUSED, STATE_UNAVAILABLE, STATE_IDLE, STATE_UNKNOWN]:
            return ICON_DEFAULT

        if self._muted:
            return ICON_MUTED

        if self._source == "Bluetooth":
            return ICON_BLUETOOTH

        if self._playing_connect or (self._playing_stream and not self._playing_mediabrowser):
            return ICON_PUSHSTREAM

        if self._state == STATE_PLAYING:
            return ICON_PLAYING

        return ICON_DEFAULT	


    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return int(self._volume) / MAX_VOL

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return self._muted

    @property
    def source(self):
        """Return the current input source."""
        if self._source not in ['Idle', 'Network']:
            return self._source
        else:
            return None

    @property
    def source_list(self):
        """Return the list of available input sources."""
        source_list = self._source_list.copy()

        if self._device_model == 'WiiM Mini' and 'optical' in source_list:
            del source_list['optical']

        if self._device_model != 'WiiM Amp' and 'HDMI' in source_list:
            del source_list['HDMI']

        if len(source_list) > 0:
            return list(source_list.values())
        else:
            return None

    @property
    def supported_features(self):
        """Flag media player features that are supported."""

        if self._playing_connect:
            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                self._features = \
                MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SELECT_SOURCE | \
                MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
                MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.SHUFFLE_SET | MediaPlayerEntityFeature.REPEAT_SET | MediaPlayerEntityFeature.SEEK | \
                MediaPlayerEntityFeature.VOLUME_MUTE
            else:
                self._features = \
                MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SELECT_SOURCE | \
                MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
                MediaPlayerEntityFeature.NEXT_TRACK | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.SHUFFLE_SET | MediaPlayerEntityFeature.REPEAT_SET | \
                MediaPlayerEntityFeature.VOLUME_MUTE
        elif self._playing_stream:
            self._features = \
            MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SELECT_SOURCE | \
            MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE | \
            MediaPlayerEntityFeature.VOLUME_MUTE
            if not self._playing_mediabrowser:
                self._features |= MediaPlayerEntityFeature.NEXT_TRACK 
                self._features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
                self._features |= MediaPlayerEntityFeature.SHUFFLE_SET
                self._features |= MediaPlayerEntityFeature.REPEAT_SET				

        elif self._playing_liveinput:
            self._features = \
            MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SELECT_SOURCE | \
            MediaPlayerEntityFeature.STOP | \
            MediaPlayerEntityFeature.VOLUME_MUTE
        else:
            self._features = \
            MediaPlayerEntityFeature.BROWSE_MEDIA | MediaPlayerEntityFeature.SELECT_SOURCE | \
            MediaPlayerEntityFeature.VOLUME_MUTE

        if self._features is not None and self._fixed_volume == '0':
            self._features |= MediaPlayerEntityFeature.VOLUME_SET
            self._features |= MediaPlayerEntityFeature.VOLUME_STEP

        return self._features	
		
    @property
    def media_position(self):
        """Time in seconds of current playback head position."""
        if (self._playing_connect or self._playing_stream) and self._state != STATE_UNAVAILABLE:
            return self._playhead_position
        else:
            return None

    @property
    def media_duration(self):
        """Time in seconds of current song duration."""
        if (self._playing_connect or self._playing_stream) and self._state != STATE_UNAVAILABLE:
            return self._duration
        else:
            return None

    @property
    def media_position_updated_at(self):
        """When the seek position was last updated."""
        if not self._playing_liveinput and self._state == STATE_PLAYING:
            return self._position_updated_at
        else:
            return None

    @property
    def shuffle(self):
        """Return True if shuffle mode is enabled."""
        return self._shuffle

    @property
    def repeat(self):
        """Return repeat mode."""
        return self._repeat

    @property
    def media_title(self):
        """Return title of the current track."""
        return self._media_title

    @property
    def media_artist(self):
        """Return name of the current track artist."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return name of the current track album."""
        return self._media_album

    @property
    def media_image_url(self):
        """Return name the image for the current track."""
        return self._media_image_url

    @property
    def media_content_type(self):
        """Content type of current playing media. Has to be MediaType.MUSIC in order for Lovelace to show both artist and title."""
        return MediaType.MUSIC		

		
    @property
    def device_class(self) -> MediaPlayerDeviceClass:
        return MediaPlayerDeviceClass.SPEAKER

    @property
    def extra_state_attributes(self):
        attributes = {}

        #attributes[ATTR_SAMPLERATE] = ''
        #attributes[ATTR_BITRATE] = ''
        #attributes[ATTR_DEPTH] = ''

        if self._media_uri:
            attributes[ATTR_STURI] = self._media_uri
        if self._media_uri_final:
            attributes[ATTR_STURI] = self._media_uri_final
        if self._pl_tracks:
            attributes[ATTR_PL_TRACKS] = self._pl_tracks
        if self._pl_trackc:
            attributes[ATTR_PL_TRCRT] = self._pl_trackc
        if self._trackc:
            attributes[ATTR_TRCRT] = self._trackc
        if self._uuid != '':
            attributes[ATTR_UUID] = self._uuid
        if self._samplerate:
            attributes[ATTR_SAMPLERATE] = str(float(self._samplerate) / 1000) + ' kHz'
        if self._bitrate:
            attributes[ATTR_BITRATE] = self._bitrate + ' kbps'
        if self._bitdepth and int(self._bitdepth) > 24:
            attributes[ATTR_DEPTH] = '24'
        elif self._bitdepth:
            attributes[ATTR_DEPTH] = self._bitdepth
        if self._fixed_volume:
            attributes[ATTR_FIXED_VOL] = self._fixed_volume
			
        if self._slave:
            attributes[ATTR_SLAVE] = self._slave
			
        if self._master_uuid:
            attributes[ATTR_MASTER_UUID] = self._master_uuid

        if self._media_image_url:
            attributes[ATTR_ART_URL] = self._media_image_url

        if DEBUGSTR_ATTR:
            atrdbg = ""

            if self._playing_connect:
                atrdbg = atrdbg + " _playing_connect"			

            if self._playing_stream:
                atrdbg = atrdbg + " _playing_stream"

            if self._playing_liveinput:
                atrdbg = atrdbg + " _playing_liveinput"
                
            if self._playing_mediabrowser:
                atrdbg = atrdbg + " _playing_mediabrowser"

            attributes[ATTR_DEBUG] = atrdbg

        if self._state != STATE_UNAVAILABLE:
            attributes[ATTR_FWVER] = self._fw_ver
            attributes[ATTR_DEVMODEL] = self._device_model

			
        return attributes	

    @property
    def host(self):
        """Self ip."""
        return self._host

    @property
    def track_count(self):
        """List of tracks present on the device."""
        return self._pl_tracks

    @property
    def unique_id(self):
        """Return the unique id."""
        if self._uuid != '':
            return "wiim_media_" + self._uuid

    @property
    def fw_ver(self):
        """Return the firmware version number of the device."""
        return self._fw_ver		

    @property
    def device_model(self):
        return self._device_model

    @property
    def fixed_vol(self):
        """Return the fixed volume option."""
        return self._fixed_volume

    async def async_media_next_track(self):
        """Send media_next command to media player."""

        value = await self.call_wiim_httpapi("setPlayerCmd:next", None)
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = utcnow()
        self._trackc = None
        self._wait_for_mcu = 2
        if value != "OK":
            _LOGGER.warning("Failed skip to next track. Device: %s, Got response: %s", self.entity_id, value)


    async def async_media_previous_track(self):
        """Send media_previous command to media player."""

        value = await self.call_wiim_httpapi("setPlayerCmd:prev", None)
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = utcnow()
        self._trackc = None
        self._wait_for_mcu = 2
        if value != "OK":
            _LOGGER.warning("Failed to skip to previous track." " Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_play(self):
        """Send media_play command to media player."""
        if self._state == STATE_PAUSED:
            value = await self.call_wiim_httpapi("setPlayerCmd:resume", None)
        elif self._prev_source != None:
            temp_source = next((k for k in self._source_list if self._source_list[k] == self._prev_source), None)
            if temp_source == None:
                return

            value = await self.call_wiim_httpapi("setPlayerCmd:play", None)
        else:
            value = await self.call_wiim_httpapi("setPlayerCmd:play", None)

        if value == "OK":
            self._state = STATE_PLAYING
            self._unav_throttle = False

            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
    
        else:
            _LOGGER.warning("Failed to start or resume playback. Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_pause(self):
        """Send media_pause command to media player."""

        if self._playing_liveinput and not self._playing_mediabrowser:
            # Pausing a live stream will cause a buffer overrun in hardware. Stop is the correct procedure in this case.
            # If the stream is configured as an input source, when pressing Play after this, it will be started again (using self._prev_source).
            await self.async_media_stop()
            return

        value = await self.call_wiim_httpapi("setPlayerCmd:pause", None)
        if value == "OK":
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            if self._playing_connect:
                self._connect_paused_at = utcnow()
            self._state = STATE_PAUSED

        else:
            _LOGGER.warning("Failed to pause playback. Device: %s, Got response: %s", self.entity_id, value)


    async def async_media_stop(self):
        """Send stop command."""
 
        if self._playing_connect or self._playing_liveinput or self._playing_stream:
            await self.call_wiim_httpapi("setPlayerCmd:pause", None)
            await self.call_wiim_httpapi("setPlayerCmd:switchmode:wifi", None)


        value = await self.call_wiim_httpapi("setPlayerCmd:stop", None)
        if value == "OK":
            self._state = STATE_IDLE
            self._playhead_position = 0
            self._duration = 0
            self._media_title = None
            self._prev_source = self._source
            self._source = None
 
            self._media_artist = None
            self._media_album = None

            self._media_uri = None
            self._media_uri_final = None
            self._media_source_uri = None

            self._playing_mediabrowser = False
            self._playing_stream = False
            self._playing_connect = False
            self._trackc = None
            self._media_image_url = None
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            self._connect_paused_at = None
            self._samplerate = None
            self._bitrate = None
            self._bitdepth = None

        else:
            _LOGGER.warning("Failed to stop playback. Device: %s, Got response: %s", self.entity_id, value)

    async def async_media_seek(self, position):
        """Send media_seek command to media player."""
        _LOGGER.debug("Seek. Device: %s, DUR: %s POS: %", self.name, self._duration, position)
        if self._duration > 0 and position >= 0 and position <= self._duration:
            value = await self.call_wiim_httpapi("setPlayerCmd:seek:{0}".format(str(position)), None)
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            self._wait_for_mcu = 0.2
            if value != "OK":
                _LOGGER.warning("Failed to seek. Device: %s, Got response: %s", self.entity_id, value)		

    async def async_clear_playlist(self):
        """Clear players playlist."""
        pass

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Play media from a URL or localfile."""
        _LOGGER.debug("Trying to play media. Device: %s, Media_type: %s, Media_id: %s", self.entity_id, media_type, media_id)

        self._playing_mediabrowser = False

        if not (media_type in [MediaType.MUSIC, MediaType.URL] or media_source.is_media_source_id(media_id)):
            _LOGGER.warning("For: %s Invalid media type %s. Only %s and %s is supported", self._name, media_type, MediaType.MUSIC, MediaType.URL)
            await self.async_media_stop()
            return False
            

        if media_source.is_media_source_id(media_id):
            play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            if media_id.find('radio_browser') != -1:  # radios are an exception, be treated by server redirect checker
                self._playing_mediabrowser = False
            else:
                self._playing_mediabrowser = True

            if media_id.find('media_source/local') != -1:
                self._media_source_uri = media_id
            else:
                self._media_source_uri = None

            media_id = play_item.url
            if not play_item.mime_type in ['audio/basic',
                                           'audio/mpeg', 
                                           'audio/mp3', 
                                           'audio/mpeg3', 
                                           'audio/x-mpeg-3',
                                           'audio/x-mpegurl', 
                                           'audio/mp4', 
                                           'audio/aac', 
                                           'audio/x-aac',
                                           'audio/x-hx-aac-adts', 
                                           'audio/x-aiff', 
                                           'audio/ogg', 
                                           'audio/vorbis', 
                                           'application/ogg', 
                                           'audio/opus', 
                                           'audio/webm', 
                                           'audio/wav', 
                                           'audio/x-wav', 
                                           'audio/vnd.wav', 
                                           'audio/flac',
                                           'audio/x-flac', 
                                           'audio/x-ms-wma']:
                _LOGGER.warning("For: %s Invalid media type, %s is not supported", self._name, play_item.mime_type)
                self._playing_mediabrowser = False
                return False
                
            media_id = async_process_play_media_url(self.hass, media_id)
            _LOGGER.debug("Trying to play HA media. Device: %s, Play_Item: %s, Media_id: %s", self._name, play_item, media_id)

        media_id_check = media_id.lower()

        if media_id_check.startswith('http'):
            media_type = MediaType.URL


        if not media_type in [MediaType.URL]:
            _LOGGER.warning("For: %s Invalid media type %s. Only %s is supported", self._name, media_type, MediaType.URL)
            await self.async_media_stop()
            self._playing_mediabrowser = False
            return False


        if self._playing_mediabrowser:
            media_id_final = media_id
        else:
            media_id_final = await self.async_detect_stream_url_redirection(media_id)

        if self._state == STATE_PLAYING:
            await self.call_wiim_httpapi("setPlayerCmd:pause", None)
                
        if self._playing_connect:  # disconnect from Spotify before playing new http source
            await self.call_wiim_httpapi("setPlayerCmd:switchmode:wifi", None)


        if media_id_check.endswith('.m3u') or media_id_check.endswith('.m3u8'):
            _LOGGER.debug("For: %s, Detected M3U list: %s, Media_id: %s", self._name, media_id_final, media_id)
            
            if await self.async_validate_m3u_url(media_id_final):
                value = await self.call_wiim_httpapi("setPlayerCmd:playlist:{0}:0".format(media_id_final), None)
            else:
                self._playing_mediabrowser = False
                return False
        else:
            value = await self.call_wiim_httpapi("setPlayerCmd:play:{0}".format(media_id_final), None)
        if value != "OK":
            _LOGGER.warning("Failed to play media type URL. Device: %s, Got response: %s, Media_Id: %s", self.entity_id, value, media_id)
            self._playing_mediabrowser = False
            return False


        self._state = STATE_PLAYING
        if media_id.find('tts_proxy') != -1:
            #_LOGGER.debug("Setting TTS: %s, %s", self.entity_id, self._name)
            self._playing_mediabrowser = False
            self._playing_stream = False

        self._media_title = None
        self._media_artist = None
        self._media_album = None
 
        self._playhead_position = 0
        self._duration = 0
        self._trackc = None
        self._position_updated_at = utcnow()
        self._idletime_updated_at = self._position_updated_at
        self._media_image_url = None
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None

        self._unav_throttle = False

        self._media_uri = media_id
        self._media_uri_final = media_id_final

        return True

    async def async_select_source(self, source):
        """Select input source."""

        temp_source = next((k for k in self._source_list if self._source_list[k] == source), None)
        if temp_source == None:
            return

        if self._playing_connect:  
            await self.call_wiim_httpapi("setPlayerCmd:pause", None)
            await self.call_wiim_httpapi("setPlayerCmd:switchmode:wifi", None)


        if len(self._source_list) > 0:
            prev_source = next((k for k in self._source_list if self._source_list[k] == self._source), None)

        self._unav_throttle = False

        value = await self.call_wiim_httpapi("setPlayerCmd:switchmode:{0}".format(temp_source), None)
        if value == "OK":
            self._state = STATE_PLAYING
            self._source = source
            self._media_uri = None
            self._media_uri_final = None
            self._playhead_position = 0
            self._duration = 0
            self._trackc = None
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
        else:
            _LOGGER.warning("Failed to select source. Device: %s, Got response: %s", self.entity_id, value)



		
    async def async_set_shuffle(self, shuffle):
        """Change the shuffle mode."""
        self._shuffle = shuffle
        if shuffle:
            if self._repeat == RepeatMode.OFF:
                mode = '3'
            elif self._repeat == RepeatMode.ALL:
                mode = '2'
            elif self._repeat == RepeatMode.ONE:
                mode = '3' #'5' is buggy
        else:
            if self._repeat == RepeatMode.OFF:
                mode = '4'
            elif self._repeat == RepeatMode.ALL:
                mode = '0'
            elif self._repeat == RepeatMode.ONE:
                mode = '1'
        value = await self.call_wiim_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
        if value != "OK":
            _LOGGER.warning("Failed to change shuffle mode. Device: %s, Got response: %s", self.entity_id, value)


    async def async_set_repeat(self, repeat):
        """Change the repeat mode."""
        #_LOGGER.debug("Setting repeat: %s on %s, %s", repeat, self.entity_id, self._name) 
        self._repeat = repeat
        if repeat == RepeatMode.OFF:
            mode = '3' if self._shuffle else '4'
        elif repeat == RepeatMode.ALL:
            mode = '2' if self._shuffle else '0'
        elif repeat == RepeatMode.ONE:
            mode = '3' if self._shuffle else '1' #'5' is buggy
        value = await self.call_wiim_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
        if value != "OK":
            _LOGGER.warning("Failed to change repeat mode. Device: %s, Got response: %s", self.entity_id, value)


    async def async_volume_up(self):
        """Increase volume one step"""

        if self._fixed_volume == '1':
            return
        if int(self._volume) == 100 and not self._muted:
            return

        volume = int(self._volume) + int(self._volume_step)
        if volume > 100:
            volume = 100

        await self.async_volume(volume)


    async def async_volume_down(self):
        """Decrease volume one step."""

        if self._fixed_volume == '1':
            return
        if int(self._volume) == 0:
            return

        volume = int(self._volume) - int(self._volume_step)
        if volume < 0:
            volume = 0


        await self.async_volume(volume)


    async def async_set_volume_level(self, volume):
        """Set volume level, input range 0..1, WiiM device 0..100."""

        if self._fixed_volume == '1':
            return
        volume = str(round(int(volume * MAX_VOL)))

        await self.async_volume(volume)

    async def async_mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""

        value = await self.call_wiim_httpapi("setPlayerCmd:mute:{0}".format(str(int(mute))), None)
            
        if value == "OK":
            self._muted = bool(int(mute))
        else:
            _LOGGER.warning("Failed mute/unmute volume. Device: %s, Got response: %s", self.entity_id, value)




    async def async_detect_stream_url_redirection(self, uri):
        if uri.find('tts_proxy') != -1: # skip redirect check for local TTS streams
            return uri
        _LOGGER.debug('For: %s detect URI redirect-from:   %s', self._name, uri)
        redirect_detect = True
        check_uri = uri
        try:
            while redirect_detect:
                response_location = requests.head(check_uri, allow_redirects=False, headers={'User-Agent': 'VLC/3.0.16 LibVLC/3.0.16'})
                #_LOGGER.debug('For: %s detecting URI redirect code: %s', self._name, str(response_location.status_code))
                if response_location.status_code in [301, 302, 303, 307, 308] and 'Location' in response_location.headers:
                    #_LOGGER.debug('For: %s detecting URI redirect location: %s', self._name, response_location.headers['Location'])
                    check_uri = response_location.headers['Location']
                else:
                    #_LOGGER.debug('For: %s detecting URI redirect - result: %s', self._name, check_uri)
                    redirect_detect = False
        except:
            pass

        _LOGGER.debug('For: %s detect URI redirect - to:   %s', self._name, check_uri)
        return check_uri
	
		
    async def async_validate_m3u_url(self, playlist):
        """Validate an M3U playlist URL for actual streams"""
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(10):
                response = await websession.get(playlist, ssl=False)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "For: %s unable to get the M3U playlist: %s", self._name, playlist
            )
            return False

        if response.status == HTTPStatus.OK:
            data = await response.text()
            _LOGGER.debug("For: %s M3U playlist: %s  contents: %s", self._name, playlist, data)

            lines = [line.strip("\n\r") for line in data.split("\n") if line.strip("\n\r") != ""]
            if len(lines) > 0:
                _LOGGER.debug("For: %s M3U playlist: %s  lines: %s", self._name, playlist, lines)
                noturls = [u for u in lines if not u.startswith('http')]
                _LOGGER.debug("For: %s M3U playlist: %s  not urls: %s", self._name, playlist, noturls)
                if len(noturls) > 0:
                    return False
                else:
                    return True
            else:
                _LOGGER.error("For: %s M3U playlist: %s No content to parse!!!", self._name, playlist)
                return False
        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )
            return False

        return False		
		
		
	
		

    async def async_set_media_title(self, title):
        """Set the media title property."""
        self._media_title = title

    async def async_set_media_artist(self, artist):
        """Set the media artist property."""
        self._media_artist = artist

    async def async_set_volume(self, volume):
        """Set the volume property."""
        self._volume = volume

    async def async_set_muted(self, mute):
        """Set the muted property."""
        self._muted = mute

    async def async_set_state(self, state):
        """Set the state property."""
        self._state = state


    async def async_set_playhead_position(self, position):
        """Set the playhead position property."""
        self._playhead_position = position

    async def async_set_duration(self, duration):
        """Set the duration property."""
        self._duration = duration

    async def async_set_position_updated_at(self, time):
        """Set the position updated at property."""
        self._position_updated_at = time

    async def async_set_source(self, source):
        """Set the source property."""
        self._source = source


    async def async_set_media_image_url(self, url):
        """Set the media image URL property."""
        self._media_image_url = url

    async def async_set_media_uri(self, uri):
        """Set the media URL property."""
        self._media_uri = uri		
		
    async def async_set_features(self, features):
        """Set the self features property."""
        self._features = features

    async def async_set_wait_for_mcu(self, wait_for_mcu):
        """Set the wait for mcu processing duration property."""
        self._wait_for_mcu = wait_for_mcu

    async def async_set_unav_throttle(self, unav_throttle):
        """Set update throttle property."""
        self._unav_throttle = unav_throttle		

    async def async_preset_button(self, preset):
        """Simulate pressing a physical preset button."""
        if self._preset_key != None and preset != None:
            if int(preset) > 0 and int(preset) <= self._preset_key:
                value = await self.call_wiim_httpapi("MCUKeyShortClick:{0}".format(str(preset)), None)

                if value != "OK":
                    _LOGGER.warning("Failed to recall preset %s. " "Device: %s, Got response: %s", preset, self.entity_id, value)
            else:
                _LOGGER.warning("Wrong preset number %s. Device: %s, has to be integer between 1 and %s", preset, self.entity_id, self._preset_key)

    async def async_volume(self, volume):
        if volume != None:
            if int(volume) >= 0 and int(volume) <= 100:
                value = await self.call_wiim_httpapi("setPlayerCmd:vol:{0}".format(str(volume)), None)

                if value != "OK":
                    _LOGGER.warning("Failed to set volume %s. " "Device: %s, Got response: %s", volume, self.entity_id, value)
                else:
                    self._volume = volume
            else:
                _LOGGER.warning("Wrong volume value %s. Device: %s, has to be integer between 0 and 100", volume, self.entity_id)
				
		
    async def async_execute_command(self, command, notif):
        """Execute desired command against the player using factory API."""
        if command == 'rescan':
            self._unav_throttle = False
            self._first_update = True
            value = "Scheduled to Rescan"
        elif command == 'reboot':
            value = await self.call_wiim_httpapi("reboot", None)
        else:
            value = "No such command implemented."
            _LOGGER.warning("Player %s command: %s, result: %s", self.entity_id, command, value)

        _LOGGER.debug("Player %s executed command: %s, result: %s", self.entity_id, command, value)

        if notif:
            self.hass.components.persistent_notification.async_create("<b>Executed command:</b><br>{0}<br><b>Result:</b><br>{1}".format(command, value), title=self.entity_id)
		


		
    async def async_update_via_upnp(self):
        """Update track info via UPNP."""
        import validators

        if self._player_statdata is None: #self._player_mediainfo is None:  
            return

        media_metadata = None
        try:
            self._trackc = self._player_statdata.get('TrackURI') #self._trackc = self._player_mediainfo.get('CurrentURI')
            self._media_uri_final = self._player_statdata.get('TrackSource') #self._media_uri_final = self._player_mediainfo.get('TrackSource')
            media_metadata = self._player_statdata.get('TrackMetaData') #media_metadata = self._player_mediainfo.get('CurrentURIMetaData')
            #_LOGGER.debug("GetInfoEx for: %s, UPNP media_metadata:%s", self.entity_id, self._player_statdata)
        except:
            _LOGGER.warning("GetInfoEx UPNP error: %s", self.entity_id)

        self._media_title = None
        self._media_album = None
        self._media_artist = None
        self._media_image_url = None
        self._samplerate = None
        self._bitrate = None
        self._bitdepth = None

        if media_metadata is None:
            return

        try:
            parser = ET.XMLParser(recover=True)
            xml_tree = ET.fromstring(media_metadata.encode(), parser)
        except:
            _LOGGER.warning("XML parse error for %s, %s", media_metadata, self.entity_id)
            return

        xml_path = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item/"
        title_xml_path = "{http://purl.org/dc/elements/1.1/}title"
        artist_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}artist"
        album_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}album"
        image_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI"
        rate_hz_xml_path = "{www.wiimu.com/song/}rate_hz"
        format_s_xml_path = "{www.wiimu.com/song/}format_s"
        bitrate_xml_path = "{www.wiimu.com/song/}bitrate"

        title_node = xml_tree.find("{0}{1}".format(xml_path, title_xml_path))
        artist_node = xml_tree.find("{0}{1}".format(xml_path, artist_xml_path))
        album_node = xml_tree.find("{0}{1}".format(xml_path, album_xml_path))
        image_url_node = xml_tree.find("{0}{1}".format(xml_path, image_xml_path))
        rate_hz_node = xml_tree.find("{0}{1}".format(xml_path, rate_hz_xml_path))
        format_s_node = xml_tree.find("{0}{1}".format(xml_path, format_s_xml_path))
        bitrate_node = xml_tree.find("{0}{1}".format(xml_path, bitrate_xml_path))

        if rate_hz_node is None:
            rate_hz_node_a = xml_tree.xpath("//*[local-name()='song:rate_hz']")
            if len(rate_hz_node_a or []) > 0:
                rate_hz_node = rate_hz_node_a[0]    

        if format_s_node is None:
            format_s_node_a = xml_tree.xpath("//*[local-name()='song:format_s']")
            if len(format_s_node_a or []) > 0:
                format_s_node = format_s_node_a[0] 

        if bitrate_node is None:
            bitrate_node_a = xml_tree.xpath("//*[local-name()='song:bitrate']")		
            if len(bitrate_node_a or []) > 0:
                bitrate_node = bitrate_node_a[0] 			

        if title_node is not None:
            self._media_title = title_node.text
        if artist_node is not None:
            self._media_artist = artist_node.text
        if album_node is not None:
            self._media_album = album_node.text
        if image_url_node is not None:
            self._media_image_url = image_url_node.text
        if rate_hz_node is not None:
            self._samplerate = rate_hz_node.text
        if format_s_node is not None:
            self._bitdepth = format_s_node.text
        if bitrate_node is not None:
            self._bitrate = bitrate_node.text

        if self._media_image_url is not None:
            if not validators.url(self._media_image_url):
                self._media_image_url = None    
	
    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing helper."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )		