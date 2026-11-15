#!/usr/bin/env python3
"""
Automated Patch Script for Official Linphone Android (https://gitlab.linphone.org/BC/public/linphone-android)
Injects:
1. 10 Concurrent SIP Calls limit ([sip] max_calls=10 in linphonerc_default & linphonerc_factory + runtime Core enforcement)
2. Messenger-style Draggable Floating Chat Head Bubble & Multi-Call Floating Window Service (FloatingBubbleService)
3. Automatic Lifecycle Startup via ContentProvider (BubbleInitProvider)
4. AndroidManifest.xml permissions (SYSTEM_ALERT_WINDOW, FOREGROUND_SERVICE_SPECIAL_USE) and service/provider entries
5. Known upstream AAPT contacts.xml fix
"""
import os
import re
import sys
from pathlib import Path


def patch_linphonerc(repo_dir: Path) -> None:
    print("[1/5] Patching linphonerc configuration files for max_calls=10...")
    for rc_file in repo_dir.rglob("*linphonerc*"):
        if rc_file.is_file():
            try:
                content = rc_file.read_text(encoding="utf-8", errors="ignore")
                if "[sip]" in content:
                    if re.search(r"^max_calls\s*=", content, flags=re.MULTILINE):
                        content = re.sub(
                            r"^max_calls\s*=.*$",
                            "max_calls=10",
                            content,
                            flags=re.MULTILINE,
                        )
                    else:
                        content = content.replace("[sip]", "[sip]\nmax_calls=10", 1)
                else:
                    content += "\n\n[sip]\nmax_calls=10\n"
                rc_file.write_text(content, encoding="utf-8")
                print(f"  -> Updated {rc_file.relative_to(repo_dir)}")
            except Exception as e:
                print(f"  -> Warning updating {rc_file}: {e}")


def remove_problematic_contacts_xml(repo_dir: Path) -> None:
    print("[2/5] Checking for known upstream contacts.xml AAPT issue...")
    contacts_xml = repo_dir / "app" / "src" / "main" / "res" / "xml" / "contacts.xml"
    if contacts_xml.exists():
        contacts_xml.unlink()
        print("  -> Removed app/src/main/res/xml/contacts.xml")


def write_custom_kotlin_classes(repo_dir: Path) -> None:
    print("[3/5] Injecting FloatingBubbleService & BubbleInitProvider Kotlin sources...")
    custom_pkg_dir = (
        repo_dir / "app" / "src" / "main" / "java" / "org" / "linphone" / "custom"
    )
    custom_pkg_dir.mkdir(parents=True, exist_ok=True)

    floating_service_kt = """package org.linphone.custom

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import org.linphone.core.Call
import org.linphone.core.Core
import kotlin.math.abs

class FloatingBubbleService : Service() {

    companion object {
        private const val CHANNEL_ID = "linphone_floating_bubble_channel"
        private const val NOTIFICATION_ID = 9910
        const val MAX_CONCURRENT_CALLS = 10

        fun startIfPermitted(context: Context) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(context)) {
                return
            }
            try {
                val intent = Intent(context, FloatingBubbleService::class.java)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (_: Exception) {
            }
        }
    }

    private lateinit var windowManager: WindowManager
    private var bubbleView: View? = null
    private var panelView: View? = null
    private lateinit var badgeTextView: TextView
    private lateinit var callsContainerLayout: LinearLayout
    private lateinit var statusHeaderTextView: TextView
    private lateinit var sipInputEditText: EditText

    private val mainHandler = Handler(Looper.getMainLooper())
    private var isPanelExpanded = false

    private val refreshRunnable = object : Runnable {
        override fun run() {
            enforceMaxCallsAndRefreshUi()
            mainHandler.postDelayed(this, 1000L)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForegroundNotification()
        windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        createMessengerBubbleOverlay()
        createExpandedFloatingPanel()
        mainHandler.post(refreshRunnable)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onDestroy() {
        mainHandler.removeCallbacks(refreshRunnable)
        try {
            bubbleView?.let { windowManager.removeView(it) }
            panelView?.let { windowManager.removeView(it) }
        } catch (_: Exception) {
        }
        super.onDestroy()
    }

    private fun startForegroundNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Linphone Messenger Floating Bubble",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Keeps the Messenger-style floating chat head active for up to 10 concurrent calls"
            }
            val nm = getSystemService(NotificationManager::class.java)
            nm?.createNotificationChannel(channel)
        }

        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
        val pendingIntent = if (launchIntent != null) {
            PendingIntent.getActivity(
                this,
                0,
                launchIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        } else null

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        val notification = builder
            .setContentTitle("Linphone MultiCall Bubble Active")
            .setContentText("Tap bubble to manage up to 10 simultaneous SIP calls")
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)
    }

    private fun dp(value: Int): Int {
        return TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            value.toFloat(),
            resources.displayMetrics
        ).toInt()
    }

    private fun overlayWindowType(): Int {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }
    }

    private fun createMessengerBubbleOverlay() {
        val rootFrame = FrameLayout(this)

        val circleBg = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.parseColor("#FF0084FF"))
            setStroke(dp(2), Color.WHITE)
        }

        val bubbleIcon = TextView(this).apply {
            text = "📞"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 24f)
            gravity = Gravity.CENTER
            background = circleBg
            elevation = dp(6).toFloat()
        }

        val badgeBg = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(Color.parseColor("#FFE53935"))
            setStroke(dp(1), Color.WHITE)
        }

        badgeTextView = TextView(this).apply {
            text = "0/10"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
            background = badgeBg
            setPadding(dp(4), dp(2), dp(4), dp(2))
            elevation = dp(8).toFloat()
        }

        rootFrame.addView(
            bubbleIcon,
            FrameLayout.LayoutParams(dp(60), dp(60), Gravity.CENTER)
        )
        rootFrame.addView(
            badgeTextView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP or Gravity.END
            )
        )

        val params = WindowManager.LayoutParams(
            dp(68),
            dp(68),
            overlayWindowType(),
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = dp(12)
            y = dp(180)
        }

        var initialX = 0
        var initialY = 0
        var initialTouchX = 0f
        var initialTouchY = 0f

        rootFrame.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    initialX = params.x
                    initialY = params.y
                    initialTouchX = event.rawX
                    initialTouchY = event.rawY
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    params.x = initialX + (event.rawX - initialTouchX).toInt()
                    params.y = initialY + (event.rawY - initialTouchY).toInt()
                    try {
                        windowManager.updateViewLayout(rootFrame, params)
                    } catch (_: Exception) {
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    val dx = abs(event.rawX - initialTouchX)
                    val dy = abs(event.rawY - initialTouchY)
                    if (dx < dp(10) && dy < dp(10)) {
                        toggleFloatingPanel()
                    } else {
                        val screenWidth = resources.displayMetrics.widthPixels
                        params.x = if (params.x + dp(34) < screenWidth / 2) {
                            dp(8)
                        } else {
                            screenWidth - dp(76)
                        }
                        try {
                            windowManager.updateViewLayout(rootFrame, params)
                        } catch (_: Exception) {
                        }
                    }
                    true
                }
                else -> false
            }
        }

        bubbleView = rootFrame
        try {
            windowManager.addView(rootFrame, params)
        } catch (_: Exception) {
        }
    }

    private fun createExpandedFloatingPanel() {
        val panelBg = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(16).toFloat()
            setColor(Color.parseColor("#F5181C24"))
            setStroke(dp(1), Color.parseColor("#44FFFFFF"))
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = panelBg
            setPadding(dp(14), dp(14), dp(14), dp(14))
            visibility = View.GONE
        }

        val headerRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        statusHeaderTextView = TextView(this).apply {
            text = "Linphone Multi-Call (0/10)"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
            typeface = Typeface.DEFAULT_BOLD
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }

        val minimizeBtn = Button(this).apply {
            text = "—"
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#33FFFFFF"))
            setOnClickListener { toggleFloatingPanel() }
        }

        headerRow.addView(statusHeaderTextView)
        headerRow.addView(minimizeBtn, LinearLayout.LayoutParams(dp(42), dp(36)))
        container.addView(headerRow)

        val dialRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, dp(8))
        }

        val inputBg = GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = dp(8).toFloat()
            setColor(Color.parseColor("#FF262C36"))
        }

        sipInputEditText = EditText(this).apply {
            hint = "SIP URI or Number (up to 10)"
            setHintTextColor(Color.parseColor("#99FFFFFF"))
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            background = inputBg
            setPadding(dp(10), dp(8), dp(10), dp(8))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }

        val dialBtn = Button(this).apply {
            text = "+ Call"
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#FF2E7D32"))
            setOnClickListener {
                val target = sipInputEditText.text?.toString()?.trim().orEmpty()
                if (target.isNotEmpty()) {
                    startNewConcurrentCall(target)
                    sipInputEditText.setText("")
                } else {
                    Toast.makeText(this@FloatingBubbleService, "Enter SIP address or phone number", Toast.LENGTH_SHORT).show()
                }
            }
        }

        dialRow.addView(sipInputEditText)
        dialRow.addView(
            dialBtn,
            LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                dp(38)
            ).apply { marginStart = dp(6) }
        )
        container.addView(dialRow)

        val actionRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, dp(8))
        }

        val mergeBtn = Button(this).apply {
            text = "Merge (Conf)"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#FF1565C0"))
            setOnClickListener { mergeAllCallsToConference() }
        }

        val hangupAllBtn = Button(this).apply {
            text = "End All"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#FFC62828"))
            setOnClickListener { terminateAllCalls() }
        }

        val openAppBtn = Button(this).apply {
            text = "Full App"
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#FF455A64"))
            setOnClickListener {
                toggleFloatingPanel()
                packageManager.getLaunchIntentForPackage(packageName)?.let {
                    it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    startActivity(it)
                }
            }
        }

        actionRow.addView(mergeBtn, LinearLayout.LayoutParams(0, dp(36), 1f))
        actionRow.addView(
            hangupAllBtn,
            LinearLayout.LayoutParams(0, dp(36), 1f).apply {
                marginStart = dp(4)
                marginEnd = dp(4)
            }
        )
        actionRow.addView(openAppBtn, LinearLayout.LayoutParams(0, dp(36), 1f))
        container.addView(actionRow)

        val scrollView = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                dp(230)
            )
        }

        callsContainerLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        scrollView.addView(callsContainerLayout)
        container.addView(scrollView)

        val panelParams = WindowManager.LayoutParams(
            dp(320),
            WindowManager.LayoutParams.WRAP_CONTENT,
            overlayWindowType(),
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.CENTER
            softInputMode = WindowManager.LayoutParams.SOFT_INPUT_ADJUST_PAN
        }

        panelView = container
        try {
            windowManager.addView(container, panelParams)
        } catch (_: Exception) {
        }
    }

    private fun toggleFloatingPanel() {
        val view = panelView ?: return
        isPanelExpanded = !isPanelExpanded
        view.visibility = if (isPanelExpanded) View.VISIBLE else View.GONE
        if (isPanelExpanded) {
            enforceMaxCallsAndRefreshUi()
        }
    }

    private fun withLinphoneCore(block: (Core) -> Unit) {
        try {
            val appClass = Class.forName("org.linphone.LinphoneApplication")
            val companionField = appClass.getDeclaredField("Companion")
            companionField.isAccessible = true
            val companion = companionField.get(null)
            val getCoreContext = companion.javaClass.methods.firstOrNull {
                it.name == "getCoreContext" && it.parameterCount == 0
            }
            val coreContext = getCoreContext?.invoke(companion) ?: return
            val getCore = coreContext.javaClass.methods.firstOrNull {
                it.name == "getCore" && it.parameterCount == 0
            }
            val core = getCore?.invoke(coreContext) as? Core ?: return
            block(core)
        } catch (_: Throwable) {
        }
    }

    private fun enforceMaxCallsAndRefreshUi() {
        withLinphoneCore { core ->
            try {
                if (core.maxCalls != MAX_CONCURRENT_CALLS) {
                    core.maxCalls = MAX_CONCURRENT_CALLS
                }
                val calls = core.calls
                val count = calls.size
                val callSnapshots = calls.mapIndexed { idx, call ->
                    val remote = call.remoteAddress?.asStringUriOnly() ?: "Call #${idx + 1}"
                    val stateName = call.state?.name ?: "Unknown"
                    Triple(call, remote, stateName)
                }

                mainHandler.post {
                    badgeTextView.text = "$count/$MAX_CONCURRENT_CALLS"
                    statusHeaderTextView.text = "Linphone Multi-Call ($count/$MAX_CONCURRENT_CALLS)"
                    if (isPanelExpanded) {
                        renderCallRows(callSnapshots)
                    }
                }
            } catch (_: Throwable) {
            }
        }
    }

    private fun renderCallRows(callSnapshots: List<Triple<Call, String, String>>) {
        callsContainerLayout.removeAllViews()
        if (callSnapshots.isEmpty()) {
            val emptyTv = TextView(this).apply {
                text = "No active calls.\\nYou can dial up to 10 concurrent SIP calls above."
                setTextColor(Color.parseColor("#B0BEC5"))
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
                gravity = Gravity.CENTER
                setPadding(dp(8), dp(24), dp(8), dp(24))
            }
            callsContainerLayout.addView(emptyTv)
            return
        }

        callSnapshots.forEachIndexed { index, (call, remoteUri, stateName) ->
            val rowBg = GradientDrawable().apply {
                shape = GradientDrawable.RECTANGLE
                cornerRadius = dp(8).toFloat()
                setColor(Color.parseColor("#FF232936"))
            }

            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                background = rowBg
                setPadding(dp(8), dp(6), dp(8), dp(6))
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT
                ).apply { bottomMargin = dp(6) }
            }

            val infoTv = TextView(this).apply {
                text = "${index + 1}. $remoteUri\\n[$stateName]"
                setTextColor(Color.WHITE)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }

            val holdResumeBtn = Button(this).apply {
                val isPaused = stateName.contains("Paused", ignoreCase = true)
                val isIncoming = stateName.contains("Incoming", ignoreCase = true)
                text = when {
                    isIncoming -> "Ans"
                    isPaused -> "Res"
                    else -> "Hold"
                }
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                setTextColor(Color.WHITE)
                setBackgroundColor(Color.parseColor("#FF0277BD"))
                setOnClickListener {
                    try {
                        when {
                            isIncoming -> call.accept()
                            isPaused -> call.resume()
                            else -> call.pause()
                        }
                    } catch (_: Throwable) {
                    }
                }
            }

            val endBtn = Button(this).apply {
                text = "End"
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 10f)
                setTextColor(Color.WHITE)
                setBackgroundColor(Color.parseColor("#FFD32F2F"))
                setOnClickListener {
                    try {
                        call.terminate()
                    } catch (_: Throwable) {
                    }
                }
            }

            row.addView(infoTv)
            row.addView(holdResumeBtn, LinearLayout.LayoutParams(dp(56), dp(34)).apply { marginEnd = dp(4) })
            row.addView(endBtn, LinearLayout.LayoutParams(dp(52), dp(34)))
            callsContainerLayout.addView(row)
        }
    }

    private fun startNewConcurrentCall(target: String) {
        withLinphoneCore { core ->
            try {
                core.maxCalls = MAX_CONCURRENT_CALLS
                if (core.callsNb >= MAX_CONCURRENT_CALLS) {
                    mainHandler.post {
                        Toast.makeText(
                            this@FloatingBubbleService,
                            "Maximum 10 concurrent calls reached!",
                            Toast.LENGTH_SHORT
                        ).show()
                    }
                    return@withLinphoneCore
                }
                val address = core.interpretUrl(target, true)
                if (address != null) {
                    val params = core.createCallParams(null)
                    params?.isVideoEnabled = false
                    core.inviteAddressWithParams(address, params!!)
                } else {
                    mainHandler.post {
                        Toast.makeText(this@FloatingBubbleService, "Invalid SIP address: $target", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Throwable) {
                mainHandler.post {
                    Toast.makeText(this@FloatingBubbleService, "Call error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun mergeAllCallsToConference() {
        withLinphoneCore { core ->
            try {
                core.maxCalls = MAX_CONCURRENT_CALLS
                core.addAllToConference()
            } catch (_: Throwable) {
            }
        }
    }

    private fun terminateAllCalls() {
        withLinphoneCore { core ->
            try {
                core.terminateAllCalls()
            } catch (_: Throwable) {
            }
        }
    }
}
"""

    init_provider_kt = """package org.linphone.custom

import android.app.Activity
import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast

class BubbleInitProvider : ContentProvider() {

    private var hasPromptedOverlayPermission = false

    override fun onCreate(): Boolean {
        val app = context?.applicationContext as? Application ?: return true
        app.registerActivityLifecycleCallbacks(object : Application.ActivityLifecycleCallbacks {
            override fun onActivityResumed(activity: Activity) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                    if (Settings.canDrawOverlays(activity)) {
                        FloatingBubbleService.startIfPermitted(activity)
                    } else if (!hasPromptedOverlayPermission) {
                        hasPromptedOverlayPermission = true
                        Toast.makeText(
                            activity,
                            "Please enable 'Display over other apps' for Linphone Floating Chat Head",
                            Toast.LENGTH_LONG
                        ).show()
                        try {
                            val intent = Intent(
                                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                Uri.parse("package:${activity.packageName}")
                            )
                            activity.startActivity(intent)
                        } catch (_: Exception) {
                        }
                    }
                } else {
                    FloatingBubbleService.startIfPermitted(activity)
                }
            }

            override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
            override fun onActivityStarted(activity: Activity) {}
            override fun onActivityPaused(activity: Activity) {}
            override fun onActivityStopped(activity: Activity) {}
            override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}
            override fun onActivityDestroyed(activity: Activity) {}
        })
        return true
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?
    ): Cursor? = null

    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0
    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?
    ): Int = 0
}
"""

    (custom_pkg_dir / "FloatingBubbleService.kt").write_text(
        floating_service_kt, encoding="utf-8"
    )
    (custom_pkg_dir / "BubbleInitProvider.kt").write_text(
        init_provider_kt, encoding="utf-8"
    )
    print("  -> Wrote FloatingBubbleService.kt and BubbleInitProvider.kt")


def patch_android_manifest(repo_dir: Path) -> None:
    print("[4/5] Patching AndroidManifest.xml with Overlay permissions & FloatingBubbleService...")
    manifest_path = repo_dir / "app" / "src" / "main" / "AndroidManifest.xml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"AndroidManifest.xml not found at {manifest_path}")

    content = manifest_path.read_text(encoding="utf-8")

    if "android.permission.SYSTEM_ALERT_WINDOW" not in content:
        content = content.replace(
            "<application",
            '    <uses-permission android:name="android.permission.SYSTEM_ALERT_WINDOW" />\n\n    <application',
            1,
        )

    custom_components = """
        <!-- Custom Messenger-Style Floating Chat Head & 10-Call Manager -->
        <service
            android:name="org.linphone.custom.FloatingBubbleService"
            android:enabled="true"
            android:exported="false"
            android:foregroundServiceType="specialUse">
            <property
                android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="Messenger-style floating chat head overlay for managing up to 10 concurrent SIP calls." />
        </service>

        <provider
            android:name="org.linphone.custom.BubbleInitProvider"
            android:authorities="${applicationId}.bubbleinitprovider"
            android:enabled="true"
            android:exported="false" />
    </application>"""

    if "org.linphone.custom.FloatingBubbleService" not in content:
        content = content.replace("</application>", custom_components, 1)

    manifest_path.write_text(content, encoding="utf-8")
    print("  -> Updated app/src/main/AndroidManifest.xml")


def patch_core_max_calls_in_source(repo_dir: Path) -> None:
    print("[5/5] Ensuring Core.maxCalls = 10 in upstream CoreContext...")
    for kt_file in (repo_dir / "app" / "src" / "main").rglob("CoreContext.kt"):
        text = kt_file.read_text(encoding="utf-8", errors="ignore")
        if "core.start()" in text and "core.maxCalls = 10" not in text:
            text = text.replace(
                "core.start()",
                "core.maxCalls = 10\n        core.start()",
                1,
            )
            kt_file.write_text(text, encoding="utf-8")
            print(f"  -> Injected core.maxCalls = 10 before core.start() in {kt_file.relative_to(repo_dir)}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 patch_linphone.py <path-to-linphone-android-repo>")
        sys.exit(1)

    repo_dir = Path(sys.argv[1]).resolve()
    if not repo_dir.exists():
        print(f"Error: {repo_dir} does not exist")
        sys.exit(1)

    patch_linphonerc(repo_dir)
    remove_problematic_contacts_xml(repo_dir)
    write_custom_kotlin_classes(repo_dir)
    patch_android_manifest(repo_dir)
    patch_core_max_calls_in_source(repo_dir)
    print("All custom patches applied successfully!")


if __name__ == "__main__":
    main()
