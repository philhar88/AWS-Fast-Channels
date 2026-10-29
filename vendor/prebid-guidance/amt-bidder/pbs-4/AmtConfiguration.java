// Derived from the Guidance for Deploying a Prebid Server on AWS v1.4.0, source/loadtest/amt-bidder/AmtConfiguration.java
// (Apache License 2.0; see vendor/prebid-guidance/NOTICE). FAST Channels change (overlay O4, Prebid Server Java 4.x
// only): 4.x builds a bidder's user syncers from its YAML, so the configuration no longer passes
// UsersyncerCreator.create(externalUrl), which 4.x removed. copy-fast-ctv.sh puts this file over the guidance's when
// the Prebid Server Java tree is 4.x.
package org.prebid.server.spring.config.bidder;

import org.prebid.server.bidder.BidderDeps;
import org.prebid.server.bidder.amt.AmtBidder;
import org.prebid.server.json.JacksonMapper;
import org.prebid.server.spring.config.bidder.model.BidderConfigurationProperties;
import org.prebid.server.spring.config.bidder.util.BidderDepsAssembler;
import org.prebid.server.spring.env.YamlPropertySourceFactory;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.PropertySource;

@Configuration
@PropertySource(value = "classpath:/bidder-config/amt.yaml", factory = YamlPropertySourceFactory.class)
public class AmtConfiguration {

    private static final String BIDDER_NAME = "amt";

    @Bean("amtConfigurationProperties")
    @ConfigurationProperties("adapters.amt")
    BidderConfigurationProperties configurationProperties() {
        return new BidderConfigurationProperties();
    }

    @Bean
    BidderDeps amtBidderDeps(BidderConfigurationProperties amtConfigurationProperties, JacksonMapper mapper) {
        return BidderDepsAssembler.forBidder(BIDDER_NAME)
                .withConfig(amtConfigurationProperties)
                .bidderCreator(config -> new AmtBidder(config.getEndpoint(), mapper))
                .assemble();
    }
}
