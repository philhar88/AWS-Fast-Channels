"""Pod Decision Service (PDS): the pure rules behind ``/policy``, ``/select`` and the own book.

The fast-ctv Prebid Server module asks the PDS twice per ad break: ``/policy`` before the auction, whose answer is a
patch to the bid request (bidders, regs, device, content), and ``/select`` after it, whose answer is the pod the module
renders as one VAST 4.2 document. This package holds the wire models (:mod:`.models`, contracts/pds-*.schema.json),
the privacy mode and the patch (:mod:`.policy`), the pod rules (:mod:`.pod`), the viewer-IP rule (:mod:`.xff`), the
User-Agent device-type map (:mod:`.devicetype`), arm and holdout buckets (:mod:`.buckets`), the decision records
(:mod:`.records`) and the interface through which other modules add to the patch (:mod:`.contributors`). Nothing here
does I/O; the Lambda handlers live in ``fast_channels.functions.pds``. See docs/pds.md.
"""
