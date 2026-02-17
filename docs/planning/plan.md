<!-- SPDX-License-Identifier: BUSL-1.1 -->
# immediate:
* add license file (restr. comercial)

# short term
* make base docker image configurable
* streamline startup
  * quick - uses all defaults
  * full - select from available
* security config files
  * network access (point to firewall rules? whitelist?)
  * autoapprove software installs
* configure - user selects system defaults (for quick start)

# medium-term:
* support multiple agents/frameworks
* image adaptability - let agent suggest installs, incorperate in project base image (approve with skua)

# long term:
* separate software install from dockerfile configuration - maybe it can isntall software on it's own but making changes persistent requires editing dockerfile (shouldn't allow)
  * sollution - checkpoint bin or apt before and after agent runs and diff to get installs
* santizes communication from agent by forcing into api
  * request for web-info
  * request for software install or image modification
  * batches requests accross agents/time
* cloud native app 
  * skua acts as pod manager for agent pools
  * kubernetes luanch integration
  
  
