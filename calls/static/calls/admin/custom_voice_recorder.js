(function () {
    "use strict";

    let activeRecorder = null;

    document.addEventListener("DOMContentLoaded", function () {
        ["sample_1", "sample_2", "sample_3"].forEach(setupRecorder);
    });

    function setupRecorder(fieldName) {
        const input = document.getElementById("id_" + fieldName);

        if (!input) {
            return;
        }

        const wrapper = document.createElement("div");
        wrapper.style.marginTop = "10px";
        wrapper.style.display = "flex";
        wrapper.style.gap = "8px";
        wrapper.style.alignItems = "center";
        wrapper.style.flexWrap = "wrap";

        const recordButton = document.createElement("button");
        recordButton.type = "button";
        recordButton.textContent = "🎙 Record Voice";
        styleButton(recordButton);

        const stopButton = document.createElement("button");
        stopButton.type = "button";
        stopButton.textContent = "⏹ Stop";
        stopButton.disabled = true;
        styleButton(stopButton);

        const status = document.createElement("span");
        status.textContent = "Ready";
        status.style.fontSize = "13px";

        const preview = document.createElement("audio");
        preview.controls = true;
        preview.style.display = "none";
        preview.style.width = "300px";

        wrapper.appendChild(recordButton);
        wrapper.appendChild(stopButton);
        wrapper.appendChild(status);
        wrapper.appendChild(preview);

        input.parentNode.appendChild(wrapper);

        recordButton.addEventListener("click", async function () {

            if (activeRecorder) {
                alert("Another voice sample is currently recording.");
                return;
            }

            try {
                const stream = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        channelCount: 1,
                        echoCancellation: false,
                        noiseSuppression: false,
                        autoGainControl: false
                    }
                });

                const audioContext = new (
                    window.AudioContext ||
                    window.webkitAudioContext
                )();

                const source = audioContext.createMediaStreamSource(stream);

                const processor = audioContext.createScriptProcessor(
                    4096,
                    1,
                    1
                );

                const recordedBuffers = [];

                processor.onaudioprocess = function (event) {
                    const samples =
                        event.inputBuffer.getChannelData(0);

                    recordedBuffers.push(
                        new Float32Array(samples)
                    );
                };

                source.connect(processor);
                processor.connect(audioContext.destination);

                activeRecorder = {
                    stream: stream,
                    context: audioContext,
                    source: source,
                    processor: processor,
                    buffers: recordedBuffers,
                    sampleRate: audioContext.sampleRate
                };

                recordButton.disabled = true;
                stopButton.disabled = false;

                status.textContent = "🔴 Recording...";
            } catch (error) {
                console.error(error);

                alert(
                    "Microphone permission failed. " +
                    "Please allow microphone access."
                );
            }
        });

        stopButton.addEventListener("click", async function () {

            if (!activeRecorder) {
                return;
            }

            const recorder = activeRecorder;
            activeRecorder = null;

            recorder.processor.disconnect();
            recorder.source.disconnect();

            recorder.stream.getTracks().forEach(function (track) {
                track.stop();
            });

            await recorder.context.close();

            const combined = mergeBuffers(
                recorder.buffers
            );

            const resampled = resample(
                combined,
                recorder.sampleRate,
                16000
            );

            const wavBlob = encodeWav(
                resampled,
                16000
            );

            const fileName =
                fieldName +
                "_" +
                Date.now() +
                ".wav";

            const wavFile = new File(
                [wavBlob],
                fileName,
                {
                    type: "audio/wav"
                }
            );

            const transfer = new DataTransfer();
            transfer.items.add(wavFile);

            input.files = transfer.files;

            const previewUrl =
                URL.createObjectURL(wavBlob);

            preview.src = previewUrl;
            preview.style.display = "block";

            status.textContent =
                "✅ WAV ready - click Save";

            recordButton.disabled = false;
            stopButton.disabled = true;
        });
    }

    function styleButton(button) {
        button.style.padding = "7px 12px";
        button.style.borderRadius = "6px";
        button.style.border = "1px solid #d1d5db";
        button.style.cursor = "pointer";
    }

    function mergeBuffers(buffers) {
        let totalLength = 0;

        buffers.forEach(function (buffer) {
            totalLength += buffer.length;
        });

        const result =
            new Float32Array(totalLength);

        let offset = 0;

        buffers.forEach(function (buffer) {
            result.set(buffer, offset);
            offset += buffer.length;
        });

        return result;
    }

    function resample(
        input,
        originalRate,
        targetRate
    ) {
        if (originalRate === targetRate) {
            return input;
        }

        const ratio =
            originalRate / targetRate;

        const newLength =
            Math.round(input.length / ratio);

        const output =
            new Float32Array(newLength);

        for (let i = 0; i < newLength; i++) {

            const position = i * ratio;

            const left =
                Math.floor(position);

            const right =
                Math.min(
                    left + 1,
                    input.length - 1
                );

            const fraction =
                position - left;

            output[i] =
                input[left] * (1 - fraction) +
                input[right] * fraction;
        }

        return output;
    }

    function encodeWav(samples, sampleRate) {

        const buffer =
            new ArrayBuffer(
                44 + samples.length * 2
            );

        const view =
            new DataView(buffer);

        writeString(view, 0, "RIFF");

        view.setUint32(
            4,
            36 + samples.length * 2,
            true
        );

        writeString(view, 8, "WAVE");
        writeString(view, 12, "fmt ");

        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);

        // Mono
        view.setUint16(22, 1, true);

        view.setUint32(
            24,
            sampleRate,
            true
        );

        view.setUint32(
            28,
            sampleRate * 2,
            true
        );

        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);

        writeString(view, 36, "data");

        view.setUint32(
            40,
            samples.length * 2,
            true
        );

        let offset = 44;

        for (
            let i = 0;
            i < samples.length;
            i++
        ) {
            const sample = Math.max(
                -1,
                Math.min(1, samples[i])
            );

            view.setInt16(
                offset,
                sample < 0
                    ? sample * 0x8000
                    : sample * 0x7fff,
                true
            );

            offset += 2;
        }

        return new Blob(
            [view],
            {
                type: "audio/wav"
            }
        );
    }

    function writeString(view, offset, text) {

        for (
            let i = 0;
            i < text.length;
            i++
        ) {
            view.setUint8(
                offset + i,
                text.charCodeAt(i)
            );
        }
    }
})();