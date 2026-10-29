package com.narration.studio

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.SeekBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import com.google.android.material.button.MaterialButton
import com.google.android.material.switchmaterial.SwitchMaterial
import com.naman14.androidlame.LameBuilder
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity(), TextToSpeech.OnInitListener {

    private var tts: TextToSpeech? = null
    private var ttsReady = false

    private lateinit var etText: android.widget.EditText
    private lateinit var spLang: Spinner
    private lateinit var spVoice: Spinner
    private lateinit var swOffline: SwitchMaterial
    private lateinit var sbRate: SeekBar
    private lateinit var sbPitch: SeekBar
    private lateinit var tvRate: TextView
    private lateinit var tvPitch: TextView
    private lateinit var tvStatus: TextView
    private lateinit var progress: ProgressBar
    private lateinit var btnShare: MaterialButton
    private lateinit var rbMp3: RadioButton

    private var allVoices: List<Voice> = emptyList()
    private var langList: List<Locale> = emptyList()
    private var voiceList: List<Voice> = emptyList()

    private var lastFile: File? = null
    private var pendingParts: List<File> = emptyList()
    private var pendingLastId = ""
    private var speakLastId = ""
    private var synthesizing = false
    private var totalParts = 0

    private val io = Executors.newSingleThreadExecutor()
    private val ui = Handler(Looper.getMainLooper())

    private val createDocWav =
        registerForActivityResult(ActivityResultContracts.CreateDocument("audio/x-wav")) { uri ->
            copyLastTo(uri)
        }

    private val createDocMp3 =
        registerForActivityResult(ActivityResultContracts.CreateDocument("audio/mpeg")) { uri ->
            copyLastTo(uri)
        }

    private fun copyLastTo(uri: android.net.Uri?) {
        val f = lastFile
        if (uri == null || f == null) return
        io.execute {
            try {
                contentResolver.openOutputStream(uri)?.use { out ->
                    f.inputStream().use { it.copyTo(out) }
                }
                ui.post { toast(getString(R.string.saved_ok)) }
            } catch (e: Exception) {
                ui.post { toast(getString(R.string.error_generic)) }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        etText = findViewById(R.id.etText)
        spLang = findViewById(R.id.spLang)
        spVoice = findViewById(R.id.spVoice)
        swOffline = findViewById(R.id.swOffline)
        sbRate = findViewById(R.id.sbRate)
        sbPitch = findViewById(R.id.sbPitch)
        tvRate = findViewById(R.id.tvRate)
        tvPitch = findViewById(R.id.tvPitch)
        tvStatus = findViewById(R.id.tvStatus)
        progress = findViewById(R.id.progress)
        btnShare = findViewById(R.id.btnShare)
        rbMp3 = findViewById(R.id.rbMp3)

        sbRate.max = 150
        sbRate.progress = 50
        sbPitch.max = 150
        sbPitch.progress = 50
        updateLabels()

        val seekListener = object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar?, p: Int, fromUser: Boolean) = updateLabels()
            override fun onStartTrackingTouch(s: SeekBar?) {}
            override fun onStopTrackingTouch(s: SeekBar?) {}
        }
        sbRate.setOnSeekBarChangeListener(seekListener)
        sbPitch.setOnSeekBarChangeListener(seekListener)

        spLang.onItemSelectedListener = onSelected { refreshVoices() }
        swOffline.setOnCheckedChangeListener { _, _ -> if (ttsReady) refreshLanguages() }

        findViewById<MaterialButton>(R.id.btnPlay).setOnClickListener { speak() }
        findViewById<MaterialButton>(R.id.btnStop).setOnClickListener { stopAll() }
        findViewById<MaterialButton>(R.id.btnSave).setOnClickListener { saveToFile() }
        findViewById<MaterialButton>(R.id.btnVoices).setOnClickListener { openVoiceSettings() }
        btnShare.setOnClickListener { shareLast() }
        btnShare.isEnabled = false

        setStatus(getString(R.string.status_loading))
        tts = TextToSpeech(this, this)
    }

    override fun onResume() {
        super.onResume()
        // Refresh the voice list after returning from the voice-download screen.
        if (ttsReady) loadVoices()
    }

    override fun onDestroy() {
        tts?.stop()
        tts?.shutdown()
        io.shutdown()
        super.onDestroy()
    }

    // ---------- TTS init ----------

    override fun onInit(status: Int) {
        if (status != TextToSpeech.SUCCESS) {
            setStatus(getString(R.string.status_no_engine))
            return
        }
        ttsReady = true
        tts?.setOnUtteranceProgressListener(progressListener)
        loadVoices()
    }

    private val progressListener = object : UtteranceProgressListener() {
        override fun onStart(utteranceId: String?) {}

        override fun onDone(utteranceId: String?) {
            val id = utteranceId ?: return
            if (id.startsWith("spk_")) {
                if (id == speakLastId) ui.post { setStatus(getString(R.string.status_done)) }
            } else if (id.startsWith("syn_")) {
                val n = id.removePrefix("syn_").toIntOrNull() ?: 0
                ui.post { setStatus(getString(R.string.status_synth, minOf(n + 2, totalParts), totalParts)) }
                if (id == pendingLastId) finishSynthesis()
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onError(utteranceId: String?) {
            ui.post { failSynthesis() }
        }

        override fun onError(utteranceId: String?, errorCode: Int) {
            ui.post { failSynthesis() }
        }
    }

    // ---------- Languages and voices ----------

    private fun loadVoices() {
        val t = tts ?: return
        allVoices = try {
            t.voices?.toList() ?: emptyList()
        } catch (e: Exception) {
            emptyList()
        }
        refreshLanguages()
    }

    private fun voiceUsable(v: Voice): Boolean {
        if (!swOffline.isChecked) return true
        val notInstalled = v.features?.contains("notInstalled") == true
        return !v.isNetworkConnectionRequired && !notInstalled
    }

    private fun refreshLanguages() {
        val previousTag = langList.getOrNull(spLang.selectedItemPosition)?.toLanguageTag()
        val filtered = allVoices.filter { voiceUsable(it) }
        langList = filtered.map { it.locale }
            .distinctBy { it.toLanguageTag() }
            .sortedBy { it.displayName }

        if (langList.isEmpty()) {
            spLang.adapter = simpleAdapter(emptyList())
            spVoice.adapter = simpleAdapter(emptyList())
            voiceList = emptyList()
            setStatus(getString(R.string.status_no_voices))
            return
        }

        spLang.adapter = simpleAdapter(langList.map { it.displayName })

        val deviceLang = Locale.getDefault().language
        var idx = langList.indexOfFirst { it.toLanguageTag() == previousTag }
        if (idx < 0) idx = langList.indexOfFirst { it.language == deviceLang }
        if (idx < 0) idx = langList.indexOfFirst { it.language == "he" }
        if (idx < 0) idx = 0
        spLang.setSelection(idx)
        setStatus(getString(R.string.status_ready))
    }

    private fun refreshVoices() {
        val loc = langList.getOrNull(spLang.selectedItemPosition) ?: return
        val previousName = voiceList.getOrNull(spVoice.selectedItemPosition)?.name
        voiceList = allVoices
            .filter { it.locale.toLanguageTag() == loc.toLanguageTag() && voiceUsable(it) }
            .sortedBy { it.name }

        val labels = voiceList.mapIndexed { i, v ->
            val net = if (v.isNetworkConnectionRequired) getString(R.string.online) else getString(R.string.offline)
            val hq = if (v.quality >= Voice.QUALITY_HIGH) " · " + getString(R.string.high_quality) else ""
            "${getString(R.string.voice)} ${i + 1} · $net$hq"
        }
        spVoice.adapter = simpleAdapter(labels)
        val keep = voiceList.indexOfFirst { it.name == previousName }
        if (keep >= 0) spVoice.setSelection(keep)
    }

    private fun simpleAdapter(items: List<String>): ArrayAdapter<String> {
        val a = ArrayAdapter(this, android.R.layout.simple_spinner_item, items)
        a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        return a
    }

    private fun onSelected(block: () -> Unit) = object : AdapterView.OnItemSelectedListener {
        override fun onItemSelected(p: AdapterView<*>?, v: View?, pos: Int, id: Long) = block()
        override fun onNothingSelected(p: AdapterView<*>?) {}
    }

    // ---------- Settings ----------

    private fun rate(): Float = 0.5f + sbRate.progress / 100f
    private fun pitch(): Float = 0.5f + sbPitch.progress / 100f

    private fun updateLabels() {
        tvRate.text = getString(R.string.rate_label, String.format(Locale.US, "%.2fx", rate()))
        tvPitch.text = getString(R.string.pitch_label, String.format(Locale.US, "%.2fx", pitch()))
    }

    private fun applySettings(): Boolean {
        val t = tts ?: return false
        val v = voiceList.getOrNull(spVoice.selectedItemPosition)
        if (v != null) {
            t.setVoice(v)
        } else {
            val loc = langList.getOrNull(spLang.selectedItemPosition)
            if (loc != null) t.setLanguage(loc)
        }
        t.setSpeechRate(rate())
        t.setPitch(pitch())
        return true
    }

    // ---------- Text handling ----------

    private fun maxChunk(): Int = minOf(TextToSpeech.getMaxSpeechInputLength(), 3000)

    private fun splitText(text: String, max: Int): List<String> {
        val out = ArrayList<String>()
        val sb = StringBuilder()
        val sentences = text.split(Regex("(?<=[.!?…。！？\\n])"))
        for (s0 in sentences) {
            var s = s0
            while (s.length > max) {
                if (sb.isNotEmpty()) {
                    out.add(sb.toString())
                    sb.setLength(0)
                }
                out.add(s.substring(0, max))
                s = s.substring(max)
            }
            if (sb.length + s.length > max) {
                out.add(sb.toString())
                sb.setLength(0)
            }
            sb.append(s)
        }
        if (sb.isNotBlank()) out.add(sb.toString())
        return out.filter { it.isNotBlank() }
    }

    private fun currentText(): String = etText.text.toString().trim()

    // ---------- Actions ----------

    private fun speak() {
        val t = tts ?: return
        val text = currentText()
        if (text.isEmpty()) {
            toast(getString(R.string.empty_text))
            return
        }
        if (!applySettings()) return
        stopAll()
        val parts = splitText(text, maxChunk())
        speakLastId = "spk_${parts.size - 1}"
        setStatus(getString(R.string.status_speaking))
        parts.forEachIndexed { i, p ->
            t.speak(p, if (i == 0) TextToSpeech.QUEUE_FLUSH else TextToSpeech.QUEUE_ADD, null, "spk_$i")
        }
    }

    private fun stopAll() {
        tts?.stop()
        synthesizing = false
        progress.visibility = View.GONE
        if (ttsReady) setStatus(getString(R.string.status_ready))
    }

    private fun saveToFile() {
        val t = tts ?: return
        if (synthesizing) return
        val text = currentText()
        if (text.isEmpty()) {
            toast(getString(R.string.empty_text))
            return
        }
        if (!applySettings()) return
        t.stop()

        val dir = File(cacheDir, "out")
        dir.mkdirs()
        dir.listFiles()?.forEach { it.delete() }
        lastFile = null
        btnShare.isEnabled = false

        val parts = splitText(text, maxChunk())
        val files = parts.indices.map { File(dir, "part_$it.wav") }
        pendingParts = files
        pendingLastId = "syn_${parts.size - 1}"
        totalParts = parts.size
        synthesizing = true
        progress.visibility = View.VISIBLE
        setStatus(getString(R.string.status_synth, 1, totalParts))

        for (i in parts.indices) {
            val r = t.synthesizeToFile(parts[i], Bundle(), files[i], "syn_$i")
            if (r != TextToSpeech.SUCCESS) {
                failSynthesis()
                return
            }
        }
    }

    private fun finishSynthesis() {
        val wantMp3 = rbMp3.isChecked
        io.execute {
            try {
                val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
                val dir = File(cacheDir, "out")
                val wav = File(dir, "narration_$stamp.wav")
                mergeWavs(pendingParts, wav)
                pendingParts.forEach { it.delete() }

                var result = wav
                var mp3Failed = false
                if (wantMp3) {
                    try {
                        val mp3 = File(dir, "narration_$stamp.mp3")
                        wavToMp3(wav, mp3)
                        wav.delete()
                        result = mp3
                    } catch (t: Throwable) {
                        mp3Failed = true
                    }
                }
                lastFile = result
                val isMp3 = result.name.endsWith(".mp3")
                ui.post {
                    synthesizing = false
                    progress.visibility = View.GONE
                    btnShare.isEnabled = true
                    setStatus(getString(R.string.status_file_ready))
                    if (mp3Failed) toast(getString(R.string.mp3_failed))
                    if (isMp3) createDocMp3.launch(result.name) else createDocWav.launch(result.name)
                }
            } catch (e: Exception) {
                ui.post { failSynthesis() }
            }
        }
    }

    private fun failSynthesis() {
        if (!synthesizing) return
        synthesizing = false
        progress.visibility = View.GONE
        setStatus(getString(R.string.error_synth))
    }

    private fun shareLast() {
        val f = lastFile
        if (f == null || !f.exists()) {
            toast(getString(R.string.no_file))
            return
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", f)
        val send = Intent(Intent.ACTION_SEND).apply {
            type = if (f.name.endsWith(".mp3")) "audio/mpeg" else "audio/x-wav"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(send, getString(R.string.share_title)))
    }

    private fun openVoiceSettings() {
        val attempts = listOf(
            Intent(TextToSpeech.Engine.ACTION_INSTALL_TTS_DATA),
            Intent("com.android.settings.TTS_SETTINGS"),
            Intent(Settings.ACTION_SETTINGS)
        )
        for (i in attempts) {
            try {
                startActivity(i)
                return
            } catch (e: Exception) {
                // try the next option
            }
        }
    }

    // ---------- WAV merge ----------

    private fun findData(f: File): Pair<Long, Long> {
        RandomAccessFile(f, "r").use { r ->
            var pos = 12L
            val hdr = ByteArray(8)
            while (pos + 8 <= r.length()) {
                r.seek(pos)
                r.readFully(hdr)
                val id = String(hdr, 0, 4, Charsets.US_ASCII)
                val size = ByteBuffer.wrap(hdr, 4, 4).order(ByteOrder.LITTLE_ENDIAN).int.toLong() and 0xFFFFFFFFL
                if (id == "data") {
                    val start = pos + 8
                    val avail = r.length() - start
                    val len = if (size == 0L || size > avail) avail else size
                    return Pair(start, len)
                }
                pos += 8 + size + (size and 1L)
            }
        }
        throw IllegalStateException("No data chunk")
    }

    private fun le32(v: Long): ByteArray =
        ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(v.toInt()).array()

    private fun mergeWavs(parts: List<File>, out: File) {
        if (parts.size == 1) {
            parts[0].copyTo(out, overwrite = true)
            return
        }
        val firstStart = findData(parts[0]).first
        var total = 0L
        out.outputStream().buffered().use { o ->
            RandomAccessFile(parts[0], "r").use { r ->
                val h = ByteArray(firstStart.toInt())
                r.readFully(h)
                o.write(h)
            }
            val buf = ByteArray(64 * 1024)
            for (p in parts) {
                val (start, len) = findData(p)
                RandomAccessFile(p, "r").use { r ->
                    r.seek(start)
                    var left = len
                    while (left > 0) {
                        val n = r.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                        if (n < 0) break
                        o.write(buf, 0, n)
                        left -= n
                        total += n
                    }
                }
            }
        }
        RandomAccessFile(out, "rw").use { r ->
            r.seek(4)
            r.write(le32(out.length() - 8))
            r.seek(firstStart - 4)
            r.write(le32(total))
        }
    }

    // ---------- MP3 encode (LAME) ----------

    private fun wavToMp3(wav: File, mp3: File) {
        val (start, len) = findData(wav)
        val head = ByteArray(8)
        RandomAccessFile(wav, "r").use { r ->
            r.seek(22)
            r.readFully(head)
        }
        val bb = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
        val channels = if (bb.getShort(0).toInt() >= 2) 2 else 1
        val rate = bb.getInt(2)

        val lame = LameBuilder()
            .setInSampleRate(rate)
            .setOutChannels(channels)
            .setOutBitrate(128)
            .setOutSampleRate(rate)
            .setQuality(5)
            .build()
        try {
            val frames = 4096
            val bytesPerFrame = 2 * channels
            val inBuf = ByteArray(frames * bytesPerFrame)
            val mp3Buf = ByteArray(7200 + frames * 2)
            mp3.outputStream().buffered().use { o ->
                RandomAccessFile(wav, "r").use { r ->
                    r.seek(start)
                    var left = len
                    while (left > 0) {
                        val n = r.read(inBuf, 0, minOf(inBuf.size.toLong(), left).toInt())
                        if (n <= 0) break
                        left -= n
                        val samples = n / bytesPerFrame
                        if (samples == 0) continue
                        val sb = ByteBuffer.wrap(inBuf, 0, samples * bytesPerFrame)
                            .order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
                        val l = ShortArray(samples)
                        val rr = ShortArray(samples)
                        for (i in 0 until samples) {
                            l[i] = sb.get()
                            rr[i] = if (channels == 2) sb.get() else l[i]
                        }
                        val w = lame.encode(l, rr, samples, mp3Buf)
                        if (w > 0) o.write(mp3Buf, 0, w)
                    }
                }
                val w = lame.flush(mp3Buf)
                if (w > 0) o.write(mp3Buf, 0, w)
            }
        } finally {
            lame.close()
        }
    }

    // ---------- UI helpers ----------

    private fun setStatus(s: String) {
        tvStatus.text = s
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}
