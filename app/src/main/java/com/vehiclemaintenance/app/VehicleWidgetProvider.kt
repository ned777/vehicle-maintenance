package com.vehiclemaintenance.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.view.View
import android.widget.RemoteViews
import androidx.core.content.ContextCompat

/**
 * The 2x2 home-screen widget's "brain" — see SysMonWidgetProvider in the
 * sysmon-widget project for the fuller explanation of what an
 * AppWidgetProvider is. Each placed instance is configured to watch ONE
 * vehicle (picked in WidgetConfigActivity, since the server can hold more
 * than one car) and shows only that vehicle's overdue/due-soon items — a
 * 2x2 cell only has room for a couple of short lines.
 *
 * updatePeriodMillis is 0 (see vehicle_widget_info.xml) — the widget only
 * ever refreshes when tapped, via ACTION_REFRESH below. Tapping the
 * dedicated "Read more" row (shown when there's more than fits) instead
 * opens the app, via its own separate pending intent.
 */
class VehicleWidgetProvider : AppWidgetProvider() {

    companion object {
        const val ACTION_REFRESH = "com.vehiclemaintenance.app.ACTION_REFRESH"
        const val PREFS_NAME = "vehicle_widget"

        // A 2x2 cell only fits about two short rows — see widget_vehicle.xml.
        // If there's more than this many items, the last row becomes a
        // "Read more" link instead of a third item.
        private const val MAX_ROWS = 2

        fun refreshAllWidgets(context: Context) {
            val intent = Intent(context, VehicleWidgetProvider::class.java).setAction(ACTION_REFRESH)
            context.sendBroadcast(intent)
        }

        fun updateOneWidget(context: Context, manager: AppWidgetManager, id: Int) {
            val views = RemoteViews(context.packageName, R.layout.widget_vehicle)

            // Tapping anywhere on the widget (other than the Read More row
            // below) re-fetches from the server — this IS the refresh
            // mechanism, there's no timer.
            val refreshIntent = Intent(context, VehicleWidgetProvider::class.java).setAction(ACTION_REFRESH)
            val refreshPendingIntent = PendingIntent.getBroadcast(
                context, 0, refreshIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            views.setOnClickPendingIntent(R.id.widgetRoot, refreshPendingIntent)

            views.removeAllViews(R.id.itemsList)

            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val vehicleId = prefs.getString("widget_${id}_vehicle_id", null)
            val vehicleName = prefs.getString("widget_${id}_vehicle_name", null)

            if (vehicleId == null) {
                // Can happen if the configured vehicle's prefs were somehow
                // cleared — normally every placed widget has one, since
                // WidgetConfigActivity is required before placement finishes.
                views.setTextViewText(R.id.vehicleNameText, context.getString(R.string.app_name))
                showMessage(context, views, context.getString(R.string.widget_no_vehicle), R.color.text_dim)
                manager.updateAppWidget(id, views)
                return
            }

            views.setTextViewText(R.id.vehicleNameText, vehicleName ?: context.getString(R.string.app_name))

            val items = MaintenanceClient.fetchDueItems(vehicleId)
            when {
                items == null ->
                    showMessage(context, views, context.getString(R.string.widget_unreachable), R.color.text_dim)
                items.isEmpty() ->
                    showMessage(context, views, context.getString(R.string.widget_all_good), R.color.ok)
                else -> {
                    views.setViewVisibility(R.id.allGoodText, View.GONE)
                    views.setViewVisibility(R.id.itemsList, View.VISIBLE)

                    val overflow = items.size > MAX_ROWS
                    val visibleItemCount = if (overflow) MAX_ROWS - 1 else items.size
                    items.take(visibleItemCount).forEach { item ->
                        views.addView(R.id.itemsList, buildItemRow(context, item))
                    }
                    if (overflow) {
                        val remaining = items.size - visibleItemCount
                        views.addView(R.id.itemsList, buildReadMoreRow(context, remaining))
                    }
                }
            }

            manager.updateAppWidget(id, views)
        }

        private fun showMessage(context: Context, views: RemoteViews, message: String, colorRes: Int) {
            views.setViewVisibility(R.id.itemsList, View.GONE)
            views.setViewVisibility(R.id.allGoodText, View.VISIBLE)
            views.setTextViewText(R.id.allGoodText, message)
            views.setTextColor(R.id.allGoodText, ContextCompat.getColor(context, colorRes))
        }

        private fun buildItemRow(context: Context, item: MaintenanceItem): RemoteViews {
            val row = RemoteViews(context.packageName, R.layout.widget_item_row)
            val dotRes = if (item.status == "overdue") R.drawable.widget_led_overdue else R.drawable.widget_led_due_soon
            row.setImageViewResource(R.id.ledDot, dotRes)
            row.setTextViewText(R.id.itemLabel, item.label)
            return row
        }

        private fun buildReadMoreRow(context: Context, remaining: Int): RemoteViews {
            val row = RemoteViews(context.packageName, R.layout.widget_item_row)
            row.setImageViewResource(R.id.ledDot, R.drawable.widget_led_more)
            row.setTextViewText(R.id.itemLabel, context.getString(R.string.widget_read_more, remaining))
            row.setTextColor(R.id.itemLabel, ContextCompat.getColor(context, R.color.teal))

            // Only this row opens the app — every other tap on the widget
            // (see updateOneWidget above) triggers a refresh instead.
            val openAppPendingIntent = PendingIntent.getActivity(
                context, 0, Intent(context, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            row.setOnClickPendingIntent(R.id.itemRowRoot, openAppPendingIntent)
            return row
        }
    }

    override fun onUpdate(context: Context, appWidgetManager: AppWidgetManager, appWidgetIds: IntArray) {
        val pending = goAsync()
        Thread {
            try {
                appWidgetIds.forEach { updateOneWidget(context, appWidgetManager, it) }
            } finally {
                pending.finish()
            }
        }.start()
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION_REFRESH) {
            val pending = goAsync()
            Thread {
                try {
                    val manager = AppWidgetManager.getInstance(context)
                    val ids = manager.getAppWidgetIds(ComponentName(context, VehicleWidgetProvider::class.java))
                    ids.forEach { updateOneWidget(context, manager, it) }
                } finally {
                    pending.finish()
                }
            }.start()
        } else {
            super.onReceive(context, intent)
        }
    }

    override fun onDeleted(context: Context, appWidgetIds: IntArray) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val editor = prefs.edit()
        appWidgetIds.forEach { id ->
            editor.remove("widget_${id}_vehicle_id")
            editor.remove("widget_${id}_vehicle_name")
        }
        editor.apply()
    }
}
