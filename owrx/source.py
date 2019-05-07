import subprocess
from owrx.config import PropertyManager, FeatureDetector
import threading
import csdr
import time
import os
import signal

class RtlNmuxSource(object):
    types = {
        "rtl_sdr": {
            "command": "rtl_sdr -s {samp_rate} -f {center_freq} -p {ppm} -g {rf_gain} -",
            "format_conversion": "csdr convert_u8_f",
        },
        "hackrf": {
            "command": "hackrf_transfer -s {samp_rate} -f {center_freq} -g {rf_gain} -l{lna_gain} -a{rf_amp} -r-",
            "format_conversion": "csdr convert_s8_f"
        },
        "sdrplay": {
            "command": "rx_sdr -F CF32 -s {samp_rate} -f {center_freq} -p {ppm} -g {rf_gain} -",
            "format_conversion": None
        }
    }

    def setup(self):
        self.props = props = PropertyManager.getSharedInstance().collect(
            "rtl_type", "samp_rate", "nmux_memory", "iq_server_port", "center_freq", "ppm",
            "rf_gain", "lna_gain", "rf_amp"
        )

        def restart(name, value):
            print("would now restart rtl source due to property change: {0} changed to {1}".format(name, value))
            self.stop()
            self.start()
        props.wire(restart)

        self.start()

    def start(self):

        props = self.props

        featureDetector = FeatureDetector()
        if not featureDetector.is_available(props["rtl_type"]):
            print("The RTL source type {0} is not available. please check requirements.".format(props["rtl_type"]))
            return

        params = RtlNmuxSource.types[props["rtl_type"]]

        start_sdr_command = params["command"].format(
            samp_rate = props["samp_rate"],
            center_freq = props["center_freq"],
            ppm = props["ppm"],
            rf_gain = props["rf_gain"],
            lna_gain = props["lna_gain"],
            rf_amp = props["rf_amp"]
        )

        if params["format_conversion"] is not None:
            start_sdr_command += " | " + params["format_conversion"]

        nmux_bufcnt = nmux_bufsize = 0
        while nmux_bufsize < props["samp_rate"]/4: nmux_bufsize += 4096
        while nmux_bufsize * nmux_bufcnt < props["nmux_memory"] * 1e6: nmux_bufcnt += 1
        if nmux_bufcnt == 0 or nmux_bufsize == 0:
            print("[RtlNmuxSource] Error: nmux_bufsize or nmux_bufcnt is zero. These depend on nmux_memory and samp_rate options in config_webrx.py")
            return
        print("[RtlNmuxSource] nmux_bufsize = %d, nmux_bufcnt = %d" % (nmux_bufsize, nmux_bufcnt))
        cmd = start_sdr_command + " | nmux --bufsize %d --bufcnt %d --port %d --address 127.0.0.1" % (nmux_bufsize, nmux_bufcnt, props["iq_server_port"])
        self.process = subprocess.Popen(cmd, shell=True, preexec_fn=os.setpgrp)
        print("[RtlNmuxSource] Started rtl source: " + cmd)

        # TODO use this to monitor unexpected failures / shutdowns and react accordingly
        def wait_for_process_to_end():
            rc = self.process.wait()
            print("[RtlNmuxSource] shut down with RC={0}".format(rc))

        threading.Thread(target = wait_for_process_to_end).start()

    def stop(self):
        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)

class SpectrumThread(threading.Thread):
    sharedInstance = None
    @staticmethod
    def getSharedInstance():
        if SpectrumThread.sharedInstance is None:
            SpectrumThread.sharedInstance = SpectrumThread()
            SpectrumThread.sharedInstance.start()
        return SpectrumThread.sharedInstance

    def __init__(self):
        self.clients = []
        self.doRun = True
        super().__init__()

    def run(self):
        props = PropertyManager.getSharedInstance().collect(
            "samp_rate", "fft_size", "fft_fps", "fft_voverlap_factor", "fft_compression",
            "csdr_dynamic_bufsize", "csdr_print_bufsizes", "csdr_through", "iq_server_port"
        )

        samp_rate = props["samp_rate"]
        fft_size = props["fft_size"]
        fft_fps = props["fft_fps"]
        fft_voverlap_factor = props["fft_voverlap_factor"]

        dsp = csdr.dsp()
        dsp.nc_port = props["iq_server_port"]
        dsp.set_demodulator("fft")
        dsp.set_samp_rate(samp_rate)
        dsp.set_fft_size(fft_size)
        dsp.set_fft_fps(fft_fps)
        dsp.set_fft_averages(int(round(1.0 * samp_rate / fft_size / fft_fps / (1.0 - fft_voverlap_factor))) if fft_voverlap_factor>0 else 0)
        dsp.set_fft_compression(props["fft_compression"])
        dsp.csdr_dynamic_bufsize = props["csdr_dynamic_bufsize"]
        dsp.csdr_print_bufsizes = props["csdr_print_bufsizes"]
        dsp.csdr_through = props["csdr_through"]
        print("[openwebrx-spectrum] Spectrum thread initialized successfully.")
        dsp.start()
        if props["csdr_dynamic_bufsize"]:
            dsp.read(8) #dummy read to skip bufsize & preamble
            print("[openwebrx-spectrum] Note: CSDR_DYNAMIC_BUFSIZE_ON = 1")
        print("[openwebrx-spectrum] Spectrum thread started.")
        bytes_to_read=int(dsp.get_fft_bytes_to_read())
        while self.doRun:
            data=dsp.read(bytes_to_read)
            if len(data) == 0:
                time.sleep(1)
            else:
                for c in self.clients:
                    c.write_spectrum_data(data)

        dsp.stop()
        print("spectrum thread shut down")

    def add_client(self, c):
        self.clients.append(c)

    def remove_client(self, c):
        self.clients.remove(c)
        if not self.clients:
            self.shutdown()

    def shutdown(self):
        print("shutting down spectrum thread")
        SpectrumThread.sharedInstance = None
        self.doRun = False

class DspManager(object):
    def __init__(self, handler):
        self.doRun = True
        self.handler = handler

        self.localProps = PropertyManager.getSharedInstance().collect(
            "audio_compression", "fft_compression", "digimodes_fft_size", "csdr_dynamic_bufsize",
            "csdr_print_bufsizes", "csdr_through", "iq_server_port", "digimodes_enable", "samp_rate"
        )

        self.dsp = csdr.dsp()
        #dsp_initialized=False
        self.localProps.getProperty("audio_compression").wire(self.dsp.set_audio_compression)
        self.localProps.getProperty("fft_compression").wire(self.dsp.set_fft_compression)
        self.dsp.set_offset_freq(0)
        self.dsp.set_bpf(-4000,4000)
        self.localProps.getProperty("digimodes_fft_size").wire(self.dsp.set_secondary_fft_size)

        self.dsp.nc_port = self.localProps["iq_server_port"]
        self.dsp.csdr_dynamic_bufsize = self.localProps["csdr_dynamic_bufsize"]
        self.dsp.csdr_print_bufsizes = self.localProps["csdr_print_bufsizes"]
        self.dsp.csdr_through = self.localProps["csdr_through"]

        self.localProps.getProperty("samp_rate").wire(self.dsp.set_samp_rate)
        #do_secondary_demod=False

        self.localProps.getProperty("output_rate").wire(self.dsp.set_output_rate)
        self.localProps.getProperty("offset_freq").wire(self.dsp.set_offset_freq)
        self.localProps.getProperty("squelch_level").wire(self.dsp.set_squelch_level)

        def set_low_cut(cut):
            bpf = self.dsp.get_bpf()
            bpf[0] = cut
            self.dsp.set_bpf(*bpf)
        self.localProps.getProperty("low_cut").wire(set_low_cut)

        def set_high_cut(cut):
            bpf = self.dsp.get_bpf()
            bpf[1] = cut
            self.dsp.set_bpf(*bpf)
        self.localProps.getProperty("high_cut").wire(set_high_cut)

        def set_mod(mod):
            if (self.dsp.get_demodulator() == mod): return
            self.dsp.stop()
            self.dsp.set_demodulator(mod)
            self.dsp.start()
        self.localProps.getProperty("mod").wire(set_mod)

        if (self.localProps["digimodes_enable"]):
            def set_secondary_mod(mod):
                if mod == False: mod = None
                if self.dsp.get_secondary_demodulator() == mod: return
                self.stopSecondaryThreads()
                self.dsp.stop()
                if mod is None:
                    self.dsp.set_secondary_demodulator(None)
                else:
                    self.dsp.set_secondary_demodulator(mod)
                    self.handler.write_secondary_dsp_config({
                        "secondary_fft_size":self.localProps["digimodes_fft_size"],
                        "if_samp_rate":self.dsp.if_samp_rate(),
                        "secondary_bw":self.dsp.secondary_bw()
                    })
                self.dsp.start()

                if mod:
                    self.startSecondaryThreads()

            self.localProps.getProperty("secondary_mod").wire(set_secondary_mod)

            self.localProps.getProperty("secondary_offset_freq").wire(self.dsp.set_secondary_offset_freq)

        super().__init__()

    def start(self):
        self.dsp.start()
        threading.Thread(target = self.readDspOutput).start()
        threading.Thread(target = self.readSMeterOutput).start()

    def startSecondaryThreads(self):
        self.runSecondary = True
        self.secondaryDemodThread = threading.Thread(target = self.readSecondaryDemod)
        self.secondaryDemodThread.start()
        self.secondaryFftThread = threading.Thread(target = self.readSecondaryFft)
        self.secondaryFftThread.start()

    def stopSecondaryThreads(self):
        self.runSecondary = False
        self.secondaryDemodThread = None
        self.secondaryFftThread = None

    def readDspOutput(self):
        while (self.doRun):
            data = self.dsp.read(256)
            if len(data) != 256:
                time.sleep(1)
            else:
                self.handler.write_dsp_data(data)

    def readSMeterOutput(self):
        while (self.doRun):
            level = self.dsp.get_smeter_level()
            self.handler.write_s_meter_level(level)

    def readSecondaryDemod(self):
        while (self.runSecondary):
            data = self.dsp.read_secondary_demod(1)
            self.handler.write_secondary_demod(data)

    def readSecondaryFft(self):
        while (self.runSecondary):
            data = self.dsp.read_secondary_fft(int(self.dsp.get_secondary_fft_bytes_to_read()))
            self.handler.write_secondary_fft(data)

    def stop(self):
        self.doRun = False
        self.runSecondary = False
        self.dsp.stop()

    def setProperty(self, prop, value):
        self.localProps.getProperty(prop).setValue(value)

class CpuUsageThread(threading.Thread):
    sharedInstance = None
    @staticmethod
    def getSharedInstance():
        if CpuUsageThread.sharedInstance is None:
            CpuUsageThread.sharedInstance = CpuUsageThread()
            CpuUsageThread.sharedInstance.start()
        return CpuUsageThread.sharedInstance

    def __init__(self):
        self.clients = []
        self.doRun = True
        self.last_worktime = 0
        self.last_idletime = 0
        super().__init__()

    def run(self):
        while self.doRun:
            time.sleep(3)
            try:
                cpu_usage = self.get_cpu_usage()
            except:
                cpu_usage = 0
            for c in self.clients:
                c.write_cpu_usage(cpu_usage)
        print("cpu usage thread shut down")

    def get_cpu_usage(self):
        try:
            f = open("/proc/stat","r")
        except:
            return 0 #Workaround, possibly we're on a Mac
        line = ""
        while not "cpu " in line: line=f.readline()
        f.close()
        spl = line.split(" ")
        worktime = int(spl[2]) + int(spl[3]) + int(spl[4])
        idletime = int(spl[5])
        dworktime = (worktime - self.last_worktime)
        didletime = (idletime - self.last_idletime)
        rate = float(dworktime) / (didletime+dworktime)
        self.last_worktime = worktime
        self.last_idletime = idletime
        if (self.last_worktime==0): return 0
        return rate

    def add_client(self, c):
        self.clients.append(c)

    def remove_client(self, c):
        self.clients.remove(c)
        if not self.clients:
            self.shutdown()

    def shutdown(self):
        print("shutting down cpu usage thread")
        CpuUsageThread.sharedInstance = None
        self.doRun = False
