"""Constructs of the demand module (``modules.demand``, docs/demand.md).

- :class:`OriginGate` (``origin_waf``): the two origin secrets and the WAF rule group the guidance's web ACL
  references first.
- :class:`ImageBuild` (``image_build``): the Prebid Server image repository and its x86 CodeBuild project.
- :class:`GuidanceExecutionPolicy` (``guidance_bootstrap``): the CloudFormation execution policy of the guidance's
  dedicated CDK bootstrap.
"""
