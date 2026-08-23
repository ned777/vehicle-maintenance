package com.vehiclemaintenance.app

/**
 * Points straight at the Vehicle Maintenance Record server — this app has no
 * data of its own. Same pattern as the Documentation app: credentials are
 * baked in so there's never a login screen, and the same header is attached
 * to every WebView request so forms/POSTs authenticate transparently too.
 *
 * The actual values come from BuildConfig, generated at build time from
 * secrets.properties (gitignored — see secrets.properties.example) rather
 * than being hardcoded here, so the server address and password never end
 * up in source control.
 */
object Config {
    const val BASE_URL = BuildConfig.VMR_BASE_URL
    const val USERNAME = BuildConfig.VMR_USERNAME
    const val PASSWORD = BuildConfig.VMR_PASSWORD
}
