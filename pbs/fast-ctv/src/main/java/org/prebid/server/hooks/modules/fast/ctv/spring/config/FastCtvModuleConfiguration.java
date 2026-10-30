package org.prebid.server.hooks.modules.fast.ctv.spring.config;

import org.prebid.server.hooks.modules.fast.ctv.core.HousePod;
import org.prebid.server.hooks.modules.fast.ctv.core.PodRenderer;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyClient;
import org.prebid.server.hooks.modules.fast.ctv.core.SelectClient;
import org.prebid.server.hooks.modules.fast.ctv.core.TrackerTemplate;
import org.prebid.server.hooks.modules.fast.ctv.v1.FastCtvModule;
import org.prebid.server.hooks.modules.fast.ctv.v1.FastCtvPolicyHook;
import org.prebid.server.hooks.modules.fast.ctv.v1.FastCtvVastHook;
import org.prebid.server.json.JacksonMapper;
import org.prebid.server.log.Logger;
import org.prebid.server.log.LoggerFactory;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.function.Consumer;

/**
 * Registers the fast-ctv module when {@code hooks.fast-ctv.enabled=true}:
 * <pre>
 * hooks:
 *   fast-ctv:
 *     enabled: true
 *     policy-url: http://pds.&lt;prefix&gt;.fast.internal/policy
 *     policy-timeout-ms: 50
 *     select-url: http://pds.&lt;prefix&gt;.fast.internal/select
 *     select-timeout-ms: 100
 *     tracker-template: https://&lt;cdn&gt;/ads/b.gif?d={sid}.{evid}&amp;c={crid}&amp;e={event}&amp;enr={enr}
 *     house-ads: '[{"crid":"house1","adm":"&lt;VAST ...&gt;","dur":30,"w":1920,"h":1080}]'
 *     log-requests: true
 * </pre>
 * A missing or invalid URL stops Prebid Server at startup. Which hooks run, with which group timeouts, is decided by
 * the account's execution plan, never here.
 */
@Configuration
@ConditionalOnProperty(prefix = "hooks." + FastCtvModule.CODE, name = "enabled", havingValue = "true")
public class FastCtvModuleConfiguration {

    private static final Logger logger = LoggerFactory.getLogger(FastCtvModule.class);

    @Bean
    FastCtvModule fastCtvModule(HttpClient httpClient,
                                JacksonMapper mapper,
                                @Value("${hooks.fast-ctv.policy-url}") String policyUrl,
                                @Value("${hooks.fast-ctv.policy-timeout-ms:50}") long policyTimeoutMs,
                                @Value("${hooks.fast-ctv.select-url}") String selectUrl,
                                @Value("${hooks.fast-ctv.select-timeout-ms:100}") long selectTimeoutMs,
                                @Value("${hooks.fast-ctv.tracker-template}") String trackerTemplate,
                                @Value("${hooks.fast-ctv.house-ads:[]}") String houseAds,
                                @Value("${hooks.fast-ctv.log-requests:false}") boolean logRequests) {

        final Consumer<String> log = logRequests ? logger::info : null;
        return new FastCtvModule(
                new FastCtvPolicyHook(new PolicyClient(httpClient, mapper.mapper(), policyUrl, policyTimeoutMs),
                        mapper.mapper(), log),
                new FastCtvVastHook(new SelectClient(httpClient, mapper.mapper(), selectUrl, selectTimeoutMs),
                        new PodRenderer(), new TrackerTemplate(trackerTemplate), mapper.mapper(), log,
                        HousePod.parse(houseAds, mapper.mapper())));
    }
}
