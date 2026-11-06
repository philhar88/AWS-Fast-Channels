"""Viewer identity (the ``identity`` module, DESIGN-v2.2 §5, ADR-0018): an opaque playback ticket per play.

A signed-in viewer's player calls ``POST /playback`` with a Cognito access token. The mint writes a single-use
ticket row ``T#<t>`` (the viewer's ``vid``, privacy mode and persona, the playback configuration it is for, a
120-second redeem window) and answers the CDN playback URL with ``?ads.t=<t>``. Nothing the viewer can change, and no
token, ``vid`` or consent value, travels in a URL. The session's PRE_SESSION Function redeems the ticket, binding it
to the MediaTailor session, and the Pod Decision Service reads the row on every break.
"""
