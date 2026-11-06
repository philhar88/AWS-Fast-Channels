// FAST Channels, identity module: the edge rebuild (ADR-0018, DESIGN-v2.2 §5.2). CloudFront Functions,
// cloudfront-js-2.0, viewer request on the /v1/* behaviour.
//
// Nothing the viewer can change reaches MediaTailor's session init: the query is emptied before anything else runs,
// and an init request (/v1/master or /v1/dash) gets back exactly one value, a single well-formed playback ticket
// ads.t. Every other parameter (ads.*, aws.*, manifest.*) is dropped. /v1/session (explicit session init) is
// refused, and so is an init path that is not canonical. Other /v1/* requests (child playlists, segments, beacons)
// keep their query: MediaTailor reads player parameters at init only.
//
// A thrown error makes CloudFront answer 503, an outage for the viewer, so every error is caught and the request
// goes on emptied: an anonymous session.
//
// OPTIMIZATION(aws): W-2 FR-1 MediaTailor honours player-set aws.* at init: the edge drops all but the ticket

var TICKET = /^[A-Za-z0-9_-]{22}$/;
var INIT = /^\/v1\/(?:master\/[0-9a-f]{40}\/[A-Za-z0-9_-]{1,64}\/[A-Za-z0-9_.-]{1,64}\.m3u8|dash\/[0-9a-f]{40}\/[A-Za-z0-9_-]{1,64}\/[A-Za-z0-9_.-]{1,64}\.mpd)$/;
var INIT_PREFIX = /^\/v1\/(?:master|dash)\//;

function deny(statusCode, statusDescription) {
  return { statusCode: statusCode, statusDescription: statusDescription };
}

function handler(event) {
  var request = event.request;
  var query = request.querystring;
  request.querystring = {};
  try {
    if (request.uri.indexOf('/v1/session') === 0) {
      return deny(403, 'Forbidden');
    }
    if (INIT_PREFIX.test(request.uri)) {
      if (!INIT.test(request.uri)) {
        return deny(400, 'Bad Request');
      }
      var ticket = query['ads.t'];
      if (ticket && !ticket.multiValue && TICKET.test(ticket.value)) {
        request.querystring['ads.t'] = { value: ticket.value };
      }
      return request;
    }
    request.querystring = query;
    return request;
  } catch (error) {
    request.querystring = {};
    return request;
  }
}
