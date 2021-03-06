"""Copyright 2008 Orbitz WorldWide

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""
import csv
import math
import pytz
from datetime import datetime
import sys
import signal

from time import time, mktime
from random import shuffle
from httplib import CannotSendRequest
from urllib import urlencode
from urlparse import urlsplit, urlunsplit
from cgi import parse_qs
from cStringIO import StringIO
from multiprocessing import Process, Queue
try:
  import cPickle as pickle
except ImportError:
  import pickle

try:  # See if there is a system installation of pytz first
  import pytz
except ImportError:  # Otherwise we fall back to Graphite's bundled version
  from graphite.thirdparty import pytz

from graphite.util import getProfileByUsername, getProfile, json, unpickle

from graphite.remote_storage import HTTPConnectionWithTimeout
from graphite.logger import log
from graphite.render.evaluator import evaluateTarget
from graphite.render.attime import parseATTime
from graphite.render.functions import PieFunctions
from graphite.render.hashing import hashRequest, hashData, hashRequestWTime
from graphite.render.glyph import GraphTypes

from django.http import HttpResponse, HttpResponseServerError, HttpResponseRedirect
from django.utils.datastructures import MultiValueDict
from django.template import Context, loader
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings


def renderView(request):
  start = time()

  try:
    global_timeout_duration = getattr(settings, 'RENDER_DURATION_TIMEOUT')
  except:
    global_timeout_duration = 60

  if request.REQUEST.has_key('json_request'):
    (graphOptions, requestOptions) = parseDataOptions(request.REQUEST['json_request'])
  elif request.is_ajax() and request.method == 'POST':
    (graphOptions, requestOptions) = parseDataOptions(request.raw_post_data)
  else:
    (graphOptions, requestOptions) = parseOptions(request)

  useCache = 'noCache' not in requestOptions
  cacheTimeout = requestOptions['cacheTimeout']
  requestContext = {
    'startTime' : requestOptions['startTime'],
    'endTime' : requestOptions['endTime'],
    'localOnly' : requestOptions['localOnly'],
    'data' : []
  }
  data = requestContext['data']

  # add template to graphOptions
  try:
    user_profile = getProfile(request, allowDefault=False)
    graphOptions['defaultTemplate'] = user_profile.defaultTemplate
  except:
    graphOptions['defaultTemplate'] = "default" 

  if request.method == 'GET':
    cache_request_obj = request.GET.copy()
  else:
    cache_request_obj = request.POST.copy()

  # hack request object to add defaultTemplate param
  cache_request_obj.appendlist("template", graphOptions['defaultTemplate'])

  # First we check the request cache
  requestKey = hashRequest(cache_request_obj)
  requestHash = hashRequestWTime(cache_request_obj)
  requestContext['request_key'] = requestHash
  request_data = ""
  if request.method == "POST":
    for k,v in request.POST.items():
        request_data += "%s=%s&" % (k.replace("\t",""),v.replace("\t",""))
  else:
    request_data = request.META['QUERY_STRING']
  log.info("DEBUG:Request_meta:[%s]\t%s\t%s\t%s\t\"%s\"" %\
          (requestHash,\
            request.META['REMOTE_ADDR'],\
            request.META['REQUEST_METHOD'],\
            request_data,\
            request.META['HTTP_USER_AGENT']))
  if useCache:
    cachedResponse = cache.get(requestKey)
    if cachedResponse:
      log.cache('Request-Cache hit [%s]' % requestHash)
      log.rendering('[%s] Returned cached response in %.6f' % (requestHash, (time() - start)))
      log.info("RENDER:[%s]:Timings:Cached %.5f" % (requestHash, time() - start))
      return cachedResponse
    else:
      log.cache('Request-Cache miss [%s]' % requestHash)

  # Now we prepare the requested data
  if requestOptions['graphType'] == 'pie':
    for target in requestOptions['targets']:
      if target.find(':') >= 0:
        try:
          name,value = target.split(':',1)
          value = float(value)
        except:
          raise ValueError("Invalid target '%s'" % target)
        data.append( (name,value) )
      else:
        q = Queue(maxsize=1)
        p = Process(target = evaluateWithQueue, args = (q, requestContext, target))
        p.start()
    
        seriesList = None
        try:
            seriesList = q.get(True, global_timeout_duration)
            p.join()
        except Exception, e:
            log.info("DEBUG:[%s] got an exception on trying to get seriesList from queue, error: %s" % (requestHash,e))
            p.terminate()
            return errorPage("Failed to fetch data")

        if seriesList == None:
            log.info("DEBUG:[%s] request timed out" % requestHash)
            p.terminate()
            return errorPage("Request timed out")

        for series in seriesList:
          func = PieFunctions[requestOptions['pieMode']]
          data.append( (series.name, func(requestContext, series) or 0 ))

  elif requestOptions['graphType'] == 'line':
    # Let's see if at least our data is cached
    if useCache:
      targets = requestOptions['targets']
      startTime = requestOptions['startTime']
      endTime = requestOptions['endTime']
      dataKey = hashData(targets, startTime, endTime)
      cachedData = cache.get(dataKey)
      if cachedData:
        log.cache("Data-Cache hit [%s]" % dataKey)
      else:
        log.cache("Data-Cache miss [%s]" % dataKey)
    else:
      cachedData = None

    if cachedData is not None:
      requestContext['data'] = data = cachedData
      log.rendering("[%s] got data cache Retrieval" % requestHash)
    else: # Have to actually retrieve the data now
      # best place for multiprocessing
      log.info("DEBUG:render:[%s] targets [ %s ]" % (requestHash, requestOptions['targets']))
      start_t = time()
      for target in requestOptions['targets']:
          if not target.strip():
            continue
          t = time()
          
          q = Queue(maxsize=1)
          p = Process(target = evaluateWithQueue, args = (q, requestContext, target))
          p.start()
      
          seriesList = None
          try:
              seriesList = q.get(True, global_timeout_duration)
              p.join()
          except Exception, e:
              log.info("DEBUG:[%s] got an exception on trying to get seriesList from queue, error: %s" % (requestHash, e))
              p.terminate()
              return errorPage("Failed to fetch data")
  
          if seriesList == None:
              log.info("DEBUG:[%s] request timed out" % requestHash)
              p.terminate()
              return errorPage("Request timed out")

          data.extend(seriesList)
      log.rendering("[%s] Retrieval took %.6f" % (requestHash, (time() - start_t)))
      log.info("RENDER:[%s]:Timigns:Retrieve %.6f" % (requestHash, (time() - start_t)))

      if useCache:
        cache.add(dataKey, data, cacheTimeout)

    # If data is all we needed, we're done
    format = requestOptions.get('format')
    if format == 'csv':
      response = HttpResponse(content_type='text/csv')
      writer = csv.writer(response, dialect='excel')

      for series in data:
        for i, value in enumerate(series):
          timestamp = datetime.fromtimestamp(series.start + (i * series.step), requestOptions['tzinfo'])
          writer.writerow((series.name, timestamp.strftime("%Y-%m-%d %H:%M:%S"), value))

      return response

    if format == 'json':
      series_data = []
      if 'maxDataPoints' in requestOptions and any(data):
        startTime = min([series.start for series in data])
        endTime = max([series.end for series in data])
        timeRange = endTime - startTime
        maxDataPoints = requestOptions['maxDataPoints']
        for series in data:
          if len(set(series)) == 1 and series[0] is None: continue
          numberOfDataPoints = timeRange/series.step
          if maxDataPoints < numberOfDataPoints:
            valuesPerPoint = math.ceil(float(numberOfDataPoints) / float(maxDataPoints))
            secondsPerPoint = int(valuesPerPoint * series.step)
            # Nudge start over a little bit so that the consolidation bands align with each call
            # removing 'jitter' seen when refreshing.
            nudge = secondsPerPoint + (series.start % series.step) - (series.start % secondsPerPoint)
            series.start = series.start + nudge
            valuesToLose = int(nudge/series.step)
            for r in range(1, valuesToLose):
              del series[0]
            series.consolidate(valuesPerPoint)
            timestamps = range(int(series.start), int(series.end)+1, int(secondsPerPoint))
          else:
            timestamps = range(int(series.start), int(series.end)+1, int(series.step))
          datapoints = zip(series, timestamps)
          series_data.append(dict(target=series.name, datapoints=datapoints))
      else:
        for series in data:
          if len(set(series)) == 1 and series[0] is None: continue
          timestamps = range(int(series.start), int(series.end)+1, int(series.step))
          datapoints = zip(series, timestamps)
          series_data.append(dict(target=series.name, datapoints=datapoints))

      if 'jsonp' in requestOptions:
        response = HttpResponse(
          content="%s(%s)" % (requestOptions['jsonp'], json.dumps(series_data)),
          content_type='text/javascript')
      else:
        response = HttpResponse(content=json.dumps(series_data), content_type='application/json')

      response['Pragma'] = 'no-cache'
      response['Cache-Control'] = 'no-cache'
      return response

    if format == 'raw':
      response = HttpResponse(content_type='text/plain')
      for series in data:
        response.write( "%s,%d,%d,%d|" % (series.name, series.start, series.end, series.step) )
        response.write( ','.join(map(str,series)) )
        response.write('\n')

      log.rendering('Total rawData rendering time %.6f' % (time() - start))
      return response

    if format == 'svg':
      graphOptions['outputFormat'] = 'svg'

    if format == 'pickle':
      response = HttpResponse(content_type='application/pickle')
      seriesInfo = [series.getInfo() for series in data]
      pickle.dump(seriesInfo, response, protocol=-1)

      log.rendering('Total pickle rendering time %.6f' % (time() - start))
      return response


  start_render_time = time()
  # We've got the data, now to render it
  graphOptions['data'] = data
  if settings.REMOTE_RENDERING: # Rendering on other machines is faster in some situations
    image = delegateRendering(requestOptions['graphType'], graphOptions)
  else:
    image = doImageRender(requestOptions['graphClass'], graphOptions)
  log.info("RENDER:[%s]:Timings:imageRender %.5f" % (requestHash, time() - start_render_time))

  useSVG = graphOptions.get('outputFormat') == 'svg'
  if useSVG and 'jsonp' in requestOptions:
    response = HttpResponse(
      content="%s(%s)" % (requestOptions['jsonp'], json.dumps(image)),
      content_type='text/javascript')
  else:
    response = buildResponse(image, useSVG and 'image/svg+xml' or 'image/png')

  if useCache:
    cache.set(requestKey, response, cacheTimeout)

  log.rendering('[%s] Total rendering time %.6f seconds' % (requestHash, (time() - start)))
  log.info("RENDER:[%s]:Timings:Total %.5f" % (requestHash, time() - start))
  return response


def parseOptions(request):
  queryParams = request.REQUEST
  return parseOptionsDictionary(queryParams)


def parseDataOptions(data):
  queryParams = MultiValueDict()
  try:
    options = json.loads(data)
    for k,v in options.items():
      if isinstance(v, list):
        queryParams.setlist(k, v)
      else:
        queryParams[k] = unicode(v)
  except:
    log.exception('json_request decode error')
  return parseOptionsDictionary(queryParams)


def parseOptionsDictionary(queryParams):
  # Start with some defaults
  graphOptions = {'width' : 330, 'height' : 250}
  requestOptions = {}

  graphType = queryParams.get('graphType','line')
  assert graphType in GraphTypes, "Invalid graphType '%s', must be one of %s" % (graphType,GraphTypes.keys())
  graphClass = GraphTypes[graphType]

  # Fill in the requestOptions
  requestOptions['graphType'] = graphType
  requestOptions['graphClass'] = graphClass
  requestOptions['pieMode'] = queryParams.get('pieMode', 'average')
  requestOptions['cacheTimeout'] = int( queryParams.get('cacheTimeout', settings.DEFAULT_CACHE_DURATION) )
  requestOptions['targets'] = []

  # Extract the targets out of the queryParams
  mytargets = []
  # json_request format
  if len(queryParams.getlist('targets')) > 0:
    mytargets = queryParams.getlist('targets')

  # Normal format: ?target=path.1&target=path.2
  if len(queryParams.getlist('target')) > 0:
    mytargets = queryParams.getlist('target')

  # Rails/PHP/jQuery common practice format: ?target[]=path.1&target[]=path.2
  elif len(queryParams.getlist('target[]')) > 0:
    mytargets = queryParams.getlist('target[]')

  # Collect the targets
  for target in mytargets:
    requestOptions['targets'].append(target)

  if 'pickle' in queryParams:
    requestOptions['format'] = 'pickle'
  if 'rawData' in queryParams:
    requestOptions['format'] = 'raw'
  if 'format' in queryParams:
    requestOptions['format'] = queryParams['format']
    if 'jsonp' in queryParams:
      requestOptions['jsonp'] = queryParams['jsonp']
  if 'noCache' in queryParams:
    requestOptions['noCache'] = True
  if 'maxDataPoints' in queryParams and queryParams['maxDataPoints'].isdigit():
    requestOptions['maxDataPoints'] = int(queryParams['maxDataPoints'])

  requestOptions['localOnly'] = queryParams.get('local') == '1'

  # Fill in the graphOptions
  for opt in graphClass.customizable:
    if opt in queryParams:
      val = unicode(queryParams[opt])
      if (val.isdigit() or (val.startswith('-') and val[1:].isdigit())) and 'color' not in opt.lower():
        val = int(val)
      elif '.' in val and (val.replace('.','',1).isdigit() or (val.startswith('-') and val[1:].replace('.','',1).isdigit())):
        val = float(val)
      elif val.lower() in ('true','false'):
        val = val.lower() == 'true'
      elif val.lower() == 'default' or val == '':
        continue
      graphOptions[opt] = val

  tzinfo = pytz.timezone(settings.TIME_ZONE)
  if 'tz' in queryParams:
    try:
      tzinfo = pytz.timezone(queryParams['tz'])
    except pytz.UnknownTimeZoneError:
      pass
  requestOptions['tzinfo'] = tzinfo

  # Get the time interval for time-oriented graph types
  if graphType == 'line' or graphType == 'pie':
    if 'until' in queryParams:
      untilTime = parseATTime(queryParams['until'], tzinfo)
    else:
      untilTime = parseATTime('now', tzinfo)
    if 'from' in queryParams:
      fromTime = parseATTime(queryParams['from'], tzinfo)
    else:
      fromTime = parseATTime('-1d', tzinfo)

    startTime = min(fromTime, untilTime)
    endTime = max(fromTime, untilTime)
    assert startTime != endTime, "Invalid empty time range"

    requestOptions['startTime'] = startTime
    requestOptions['endTime'] = endTime

  return (graphOptions, requestOptions)


connectionPools = {}

def delegateRendering(graphType, graphOptions):
  start = time()
  postData = graphType + '\n' + pickle.dumps(graphOptions)
  servers = settings.RENDERING_HOSTS[:] #make a copy so we can shuffle it safely
  shuffle(servers)
  for server in servers:
    start2 = time()
    try:
      # Get a connection
      try:
        pool = connectionPools[server]
      except KeyError: #happens the first time
        pool = connectionPools[server] = set()
      try:
        connection = pool.pop()
      except KeyError: #No available connections, have to make a new one
        connection = HTTPConnectionWithTimeout(server)
        connection.timeout = settings.REMOTE_RENDER_CONNECT_TIMEOUT
      # Send the request
      try:
        connection.request('POST','/render/local/', postData)
      except CannotSendRequest:
        connection = HTTPConnectionWithTimeout(server) #retry once
        connection.timeout = settings.REMOTE_RENDER_CONNECT_TIMEOUT
        connection.request('POST', '/render/local/', postData)
      # Read the response
      response = connection.getresponse()
      assert response.status == 200, "Bad response code %d from %s" % (response.status,server)
      contentType = response.getheader('Content-Type')
      imageData = response.read()
      assert contentType == 'image/png', "Bad content type: \"%s\" from %s" % (contentType,server)
      assert imageData, "Received empty response from %s" % server
      # Wrap things up
      log.rendering('Remotely rendered image on %s in %.6f seconds' % (server,time() - start2))
      log.rendering('Spent a total of %.6f seconds doing remote rendering work' % (time() - start))
      pool.add(connection)
      return imageData
    except:
      log.exception("Exception while attempting remote rendering request on %s" % server)
      log.rendering('Exception while remotely rendering on %s wasted %.6f' % (server,time() - start2))
      continue


def renderLocalView(request):
  try:
    start = time()
    reqParams = StringIO(request.body)
    graphType = reqParams.readline().strip()
    optionsPickle = reqParams.read()
    reqParams.close()
    graphClass = GraphTypes[graphType]
    options = unpickle.loads(optionsPickle)
    image = doImageRender(graphClass, options)
    log.rendering("Delegated rendering request took %.6f seconds" % (time() -  start))
    return buildResponse(image)
  except:
    log.exception("Exception in graphite.render.views.rawrender")
    return HttpResponseServerError()


def renderMyGraphView(request,username,graphName):
  profile = getProfileByUsername(username)
  if not profile:
    return errorPage("No such user '%s'" % username)
  try:
    graph = profile.mygraph_set.get(name=graphName)
  except ObjectDoesNotExist:
    return errorPage("User %s doesn't have a MyGraph named '%s'" % (username,graphName))

  request_params = dict(request.REQUEST.items())
  if request_params:
    url_parts = urlsplit(graph.url)
    query_string = url_parts[3]
    if query_string:
      url_params = parse_qs(query_string)
      # Remove lists so that we can do an update() on the dict
      for param, value in url_params.items():
        if isinstance(value, list) and param != 'target':
          url_params[param] = value[-1]
      url_params.update(request_params)
      # Handle 'target' being a list - we want duplicate &target params out of it
      url_param_pairs = []
      for key,val in url_params.items():
        if isinstance(val, list):
          for v in val:
            url_param_pairs.append( (key,v) )
        else:
          url_param_pairs.append( (key,val) )

      query_string = urlencode(url_param_pairs)
    url = urlunsplit(url_parts[:3] + (query_string,) + url_parts[4:])
  else:
    url = graph.url
  return HttpResponseRedirect(url)


def doImageRender(graphClass, graphOptions):
  pngData = StringIO()
  t = time()
  img = graphClass(**graphOptions)
  img.output(pngData)
  log.rendering('Rendered PNG in %.6f seconds' % (time() - t))
  imageData = pngData.getvalue()
  pngData.close()
  return imageData


def buildResponse(imageData, content_type="image/png"):
  response = HttpResponse(imageData, content_type=content_type)
  response['Cache-Control'] = 'no-cache'
  response['Pragma'] = 'no-cache'
  return response


def errorPage(message):
  template = loader.get_template('500.html')
  context = Context(dict(message=message))
  return HttpResponseServerError( template.render(context) )

def evaluateWithQueue(queue, requestContext, target):
  result = evaluateTarget(requestContext, target)
  queue.put_nowait(result)
  return

