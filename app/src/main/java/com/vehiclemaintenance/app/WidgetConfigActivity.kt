package com.vehiclemaintenance.app

import android.appwidget.AppWidgetManager
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * Shown right after dragging a new Garage widget onto the home screen (see
 * android:configure in vehicle_widget_info.xml) — lets you pick WHICH
 * vehicle this specific widget instance should track, since the server can
 * hold more than one. Fetches the vehicle list from the same dashboard the
 * app itself points at (see MaintenanceClient.fetchVehicles()).
 */
class WidgetConfigActivity : AppCompatActivity() {

    private var appWidgetId = AppWidgetManager.INVALID_APPWIDGET_ID

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Very important Android convention for widget config screens:
        // default the result to CANCELED immediately. If the user backs out
        // without us ever calling setResult(RESULT_OK, ...) ourselves,
        // Android throws away the half-configured widget instead of adding
        // a broken one to the home screen.
        setResult(RESULT_CANCELED)
        setContentView(R.layout.activity_widget_config)

        appWidgetId = intent.getIntExtra(
            AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID
        )
        if (appWidgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish()
            return
        }

        loadVehicles()
    }

    private fun loadVehicles() {
        val progress = findViewById<ProgressBar>(R.id.configProgress)
        val errorText = findViewById<TextView>(R.id.configErrorText)
        val retryButton = findViewById<Button>(R.id.configRetryButton)
        val listView = findViewById<ListView>(R.id.configVehicleListView)

        progress.visibility = View.VISIBLE
        errorText.visibility = View.GONE
        retryButton.visibility = View.GONE
        listView.visibility = View.GONE

        // MaintenanceClient.fetchVehicles() is a blocking network call —
        // run it off the main thread, same reasoning as
        // VehicleWidgetProvider's own background Thread.
        Thread {
            val vehicles = MaintenanceClient.fetchVehicles()
            Handler(Looper.getMainLooper()).post {
                progress.visibility = View.GONE
                if (vehicles.isNullOrEmpty()) {
                    errorText.visibility = View.VISIBLE
                    retryButton.visibility = View.VISIBLE
                    retryButton.setOnClickListener { loadVehicles() }
                    return@post
                }

                listView.visibility = View.VISIBLE
                listView.adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, vehicles.map { it.name })
                listView.setOnItemClickListener { _, _, position, _ ->
                    val vehicle = vehicles[position]
                    getSharedPreferences(VehicleWidgetProvider.PREFS_NAME, MODE_PRIVATE)
                        .edit()
                        .putString("widget_${appWidgetId}_vehicle_id", vehicle.id)
                        .putString("widget_${appWidgetId}_vehicle_name", vehicle.name)
                        .apply()

                    // This is the crucial final step of any widget
                    // configuration screen: package the widget id back up
                    // and call setResult(RESULT_OK, ...). ONLY after this
                    // does Android go ahead and place the widget on the
                    // home screen (which triggers onUpdate() for it).
                    val resultValue = Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId)
                    setResult(RESULT_OK, resultValue)
                    finish()
                }
            }
        }.start()
    }
}
