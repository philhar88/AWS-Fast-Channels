"""Agentic selling (``modules.selling``; docs/selling.md): the stage's inventory sold over AdCP.

The Prebid Sales Agent is the seller of record, in its testing mode (a sandbox: no real money). The AWS reference
buyer and governance agents book against it. A buy it accepts becomes a direct line of the PDS own book
(:mod:`.booking`), and the line's delivery goes back to the buyer over AdCP. The products are the monetized channels
(:mod:`.products`).
"""
