package org.prebid.server.hooks.modules.fast.ctv.v1;

import org.prebid.server.hooks.v1.Hook;
import org.prebid.server.hooks.v1.InvocationContext;
import org.prebid.server.hooks.v1.Module;

import java.util.Collection;
import java.util.List;

/**
 * FAST CTV glue between MediaTailor's templated OpenRTB POST and the Pod Decision Service: a
 * processed-auction-request hook that adds third-party demand only on a positive, in-time {@code /policy} answer
 * ({@link FastCtvPolicyHook}), and an exitpoint hook that renders the pod {@code /select} returns as one VAST 4.2
 * document for MediaTailor ad buffet ({@link FastCtvVastHook}). Both fail toward the stored request: the own book and
 * house ads only. Which hooks run, and inside which group timeouts, is the account's execution plan.
 */
public class FastCtvModule implements Module {

    public static final String CODE = "fast-ctv";

    private final List<? extends Hook<?, ? extends InvocationContext>> hooks;

    public FastCtvModule(FastCtvPolicyHook policyHook, FastCtvVastHook vastHook) {
        this.hooks = List.of(policyHook, vastHook);
    }

    @Override
    public String code() {
        return CODE;
    }

    @Override
    public Collection<? extends Hook<?, ? extends InvocationContext>> hooks() {
        return hooks;
    }
}
