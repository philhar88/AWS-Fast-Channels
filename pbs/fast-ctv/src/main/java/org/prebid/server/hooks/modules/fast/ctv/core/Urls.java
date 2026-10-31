package org.prebid.server.hooks.modules.fast.ctv.core;

import java.net.URI;
import java.util.Objects;
import java.util.Set;

/**
 * Configuration URL checks, at startup. A private name such as {@code pds.<prefix>.fast.internal} is a valid host,
 * which a top-level-domain check would refuse.
 */
final class Urls {

    private static final Set<String> SCHEMES = Set.of("http", "https");

    private Urls() {
    }

    /**
     * {@code url} when it is an absolute HTTP(S) URL with a host.
     *
     * @throws IllegalArgumentException naming the setting otherwise
     */
    static String require(String url, String setting) {
        Objects.requireNonNull(url, setting);
        final URI uri;
        try {
            uri = URI.create(url);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(setting + " is not a URL: " + url, e);
        }
        if (!SCHEMES.contains(String.valueOf(uri.getScheme())) || uri.getHost() == null) {
            throw new IllegalArgumentException(setting + " must be an http or https URL with a host: " + url);
        }
        return url;
    }
}
