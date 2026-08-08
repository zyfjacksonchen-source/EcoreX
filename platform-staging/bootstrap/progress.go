package main

import (
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	interactiveProgressInterval = 150 * time.Millisecond
	logProgressInterval         = 5 * time.Second
)

type downloadProgress struct {
	Downloaded int64
	Total      int64
}

type bootstrapProgress struct {
	writer      io.Writer
	interactive bool
	now         func() time.Time

	mu              sync.Mutex
	activeLine      bool
	activeLineRunes int
	artifact        artifact
	artifactIndex   int
	artifactCount   int
	sourceLabel     string
	sourceStartedAt time.Time
	sourceStartByte int64
	lastRenderedAt  time.Time
	lastLoggedPct   int
}

type progressActivity struct {
	progress *bootstrapProgress
	stage    string
	detail   string
	started  time.Time
	stop     chan struct{}
	done     chan struct{}
	once     sync.Once
}

func newBootstrapProgress(writer io.Writer) *bootstrapProgress {
	interactive := false
	if file, ok := writer.(*os.File); ok {
		if info, err := file.Stat(); err == nil {
			interactive = info.Mode()&os.ModeCharDevice != 0
		}
	}
	return &bootstrapProgress{
		writer:        writer,
		interactive:   interactive,
		now:           time.Now,
		lastLoggedPct: -1,
	}
}

func (progress *bootstrapProgress) Stage(stage, detail string) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	fmt.Fprintf(progress.writer, "[%s] %s\n", stage, detail)
}

func (progress *bootstrapProgress) Success(detail string) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	fmt.Fprintf(progress.writer, "[完成] %s\n", detail)
}

func (progress *bootstrapProgress) BeginArtifact(
	item artifact,
	index int,
	count int,
) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	progress.artifact = item
	progress.artifactIndex = index
	progress.artifactCount = count
	progress.sourceLabel = "准备连接"
	progress.sourceStartedAt = progress.now()
	progress.sourceStartByte = 0
	progress.lastRenderedAt = time.Time{}
	progress.lastLoggedPct = -1
	progress.renderDownloadLocked(0, item.SizeBytes, true)
}

func (progress *bootstrapProgress) ArtifactCached(item artifact) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	fmt.Fprintf(
		progress.writer,
		"[校验] (%d/%d) %s 已存在，签名与摘要校验通过\n",
		progress.artifactIndex,
		progress.artifactCount,
		artifactDisplayName(item),
	)
}

func (progress *bootstrapProgress) BeginSource(
	item artifact,
	origin source,
	current int64,
) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.artifact = item
	progress.sourceLabel = sourceDisplayName(origin.Kind)
	progress.sourceStartedAt = progress.now()
	progress.sourceStartByte = current
	progress.lastRenderedAt = time.Time{}
	progress.lastLoggedPct = -1
	progress.renderDownloadLocked(current, item.SizeBytes, true)
}

func (progress *bootstrapProgress) UpdateDownload(value downloadProgress) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.renderDownloadLocked(
		value.Downloaded,
		value.Total,
		value.Downloaded >= value.Total,
	)
}

func (progress *bootstrapProgress) SourceFailed(origin source, hasFallback bool) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	if hasFallback {
		fmt.Fprintf(
			progress.writer,
			"[切换] %s暂不可用，正在尝试下一下载源\n",
			sourceDisplayName(origin.Kind),
		)
		return
	}
	fmt.Fprintf(
		progress.writer,
		"[下载] %s未能完成下载\n",
		sourceDisplayName(origin.Kind),
	)
}

func (progress *bootstrapProgress) VerifyingArtifact(item artifact) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	fmt.Fprintf(
		progress.writer,
		"[校验] (%d/%d) 正在验证 %s 的签名与 SHA-256\n",
		progress.artifactIndex,
		progress.artifactCount,
		artifactDisplayName(item),
	)
}

func (progress *bootstrapProgress) ArtifactComplete(item artifact) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	fmt.Fprintf(
		progress.writer,
		"[就绪] (%d/%d) %s 下载并校验完成\n",
		progress.artifactIndex,
		progress.artifactCount,
		artifactDisplayName(item),
	)
}

func (progress *bootstrapProgress) ArtifactRejected(hasFallback bool) {
	if progress == nil {
		return
	}
	progress.mu.Lock()
	defer progress.mu.Unlock()
	progress.finishActiveLineLocked()
	if hasFallback {
		fmt.Fprintln(
			progress.writer,
			"[校验] 当前来源的文件未通过校验，正在重新下载",
		)
		return
	}
	fmt.Fprintln(
		progress.writer,
		"[校验] 文件未通过签名或摘要校验，安装已安全停止",
	)
}

func (progress *bootstrapProgress) BeginActivity(
	stage string,
	detail string,
) *progressActivity {
	if progress == nil {
		return nil
	}
	activity := &progressActivity{
		progress: progress,
		stage:    stage,
		detail:   detail,
		started:  progress.now(),
		stop:     make(chan struct{}),
		done:     make(chan struct{}),
	}
	progress.renderActivity(activity, true)
	go activity.run()
	return activity
}

func (activity *progressActivity) run() {
	defer close(activity.done)
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-activity.stop:
			return
		case <-ticker.C:
			activity.progress.renderActivity(activity, false)
		}
	}
}

func (activity *progressActivity) End() {
	if activity == nil {
		return
	}
	activity.once.Do(func() {
		close(activity.stop)
		<-activity.done
		activity.progress.mu.Lock()
		defer activity.progress.mu.Unlock()
		activity.progress.finishActiveLineLocked()
	})
}

func (progress *bootstrapProgress) renderActivity(
	activity *progressActivity,
	force bool,
) {
	progress.mu.Lock()
	defer progress.mu.Unlock()
	now := progress.now()
	if !force && !progress.interactive &&
		now.Sub(progress.lastRenderedAt) < 10*time.Second {
		return
	}
	elapsed := now.Sub(activity.started)
	message := fmt.Sprintf(
		"[%s] %s  已用时 %s",
		activity.stage,
		activity.detail,
		formatProgressDuration(elapsed),
	)
	progress.renderActiveLineLocked(message, force)
	progress.lastRenderedAt = now
}

func (progress *bootstrapProgress) renderDownloadLocked(
	current int64,
	total int64,
	force bool,
) {
	if total <= 0 {
		return
	}
	current = min(max(current, 0), total)
	now := progress.now()
	percent := int(current * 100 / total)
	if !force {
		if progress.interactive {
			if now.Sub(progress.lastRenderedAt) < interactiveProgressInterval {
				return
			}
		} else if percent < progress.lastLoggedPct+10 &&
			now.Sub(progress.lastRenderedAt) < logProgressInterval {
			return
		}
	}
	transferred := current - progress.sourceStartByte
	elapsed := now.Sub(progress.sourceStartedAt)
	rate := float64(0)
	if transferred > 0 && elapsed > 0 {
		rate = float64(transferred) / elapsed.Seconds()
	}
	status := "正在连接"
	if rate > 0 {
		status = formatProgressBytes(int64(rate)) + "/s"
		if current < total {
			remaining := time.Duration(
				float64(total-current) / rate * float64(time.Second),
			)
			status += "  剩余 " + formatProgressDuration(remaining)
		}
	} else if current > 0 {
		status = "等待数据"
	}
	message := fmt.Sprintf(
		"[下载] (%d/%d) %s  %3d%%  %s / %s  %s  %s",
		progress.artifactIndex,
		progress.artifactCount,
		artifactDisplayName(progress.artifact),
		percent,
		formatProgressBytes(current),
		formatProgressBytes(total),
		progress.sourceLabel,
		status,
	)
	progress.renderActiveLineLocked(message, force)
	progress.lastRenderedAt = now
	progress.lastLoggedPct = percent
}

func (progress *bootstrapProgress) renderActiveLineLocked(
	message string,
	force bool,
) {
	if progress.interactive {
		padding := progress.activeLineRunes - len([]rune(message))
		if padding < 0 {
			padding = 0
		}
		fmt.Fprintf(
			progress.writer,
			"\r%s%s",
			message,
			strings.Repeat(" ", padding),
		)
		progress.activeLine = true
		progress.activeLineRunes = len([]rune(message))
		return
	}
	fmt.Fprintln(progress.writer, message)
	progress.activeLine = false
	progress.activeLineRunes = 0
}

func (progress *bootstrapProgress) finishActiveLineLocked() {
	if !progress.activeLine {
		return
	}
	if progress.interactive {
		fmt.Fprintln(progress.writer)
	}
	progress.activeLine = false
	progress.activeLineRunes = 0
}

func artifactDisplayName(item artifact) string {
	switch {
	case strings.HasPrefix(item.ArtifactID, "core-"):
		return "e-Mate 核心"
	case strings.HasPrefix(item.ArtifactID, "bootstrap-"):
		return "启动组件"
	case strings.Contains(item.ArtifactID, "office"):
		return "Office 组件"
	case strings.Contains(item.ArtifactID, "browser"):
		return "浏览器组件"
	case strings.Contains(item.ArtifactID, "ocr"):
		return "OCR 组件"
	default:
		name := strings.TrimSuffix(item.FileName, ".zip")
		if name == "" {
			return "能力组件"
		}
		return name
	}
}

func sourceDisplayName(kind string) string {
	switch kind {
	case "github-cn-mirror":
		return "国内镜像"
	case "github-release":
		return "GitHub"
	case "ecorex-cdn":
		return "e-Mate 备用源"
	default:
		return "签名下载源"
	}
}

func formatProgressBytes(value int64) string {
	if value < 0 {
		value = 0
	}
	const (
		kib = int64(1024)
		mib = 1024 * kib
		gib = 1024 * mib
	)
	switch {
	case value >= gib:
		return fmt.Sprintf("%.2f GiB", float64(value)/float64(gib))
	case value >= mib:
		return fmt.Sprintf("%.1f MiB", float64(value)/float64(mib))
	case value >= kib:
		return fmt.Sprintf("%.1f KiB", float64(value)/float64(kib))
	default:
		return fmt.Sprintf("%d B", value)
	}
}

func formatProgressDuration(value time.Duration) string {
	if value < 0 {
		value = 0
	}
	seconds := int64(value.Round(time.Second) / time.Second)
	switch {
	case seconds < 60:
		return fmt.Sprintf("%d 秒", seconds)
	case seconds < 60*60:
		return fmt.Sprintf("%d分%02d秒", seconds/60, seconds%60)
	default:
		return fmt.Sprintf(
			"%d小时%02d分",
			seconds/3600,
			(seconds%3600)/60,
		)
	}
}

func userFacingFailure(errorValue error) string {
	if errorValue == nil {
		return "安装没有完成，现有版本未被修改。请重新运行安装命令。"
	}
	message := strings.ToLower(errorValue.Error())
	switch {
	case strings.Contains(message, "acceptance checkpoint"):
		return "新版验收副本未能完成创建，现有版本和数据未被修改。请重新验收。"
	case strings.Contains(message, "locked") ||
		strings.Contains(message, "another e-mate"):
		return "另一个 e-Mate 安装或运行进程仍在工作。请等待它完成后重试。"
	case strings.Contains(message, "signature") ||
		strings.Contains(message, "verification") ||
		strings.Contains(message, "manifest") ||
		strings.Contains(message, "local release directory inventory") ||
		strings.Contains(message, "sha"):
		return "文件校验未通过，未安装不可信内容。请重新下载安装。"
	case strings.Contains(message, "discovery") ||
		strings.Contains(message, "all signed artifact sources failed") ||
		strings.Contains(message, "source did not honor") ||
		strings.Contains(message, "download"):
		return "下载未完成。请检查网络后重新运行，已下载的安全分片会继续使用。"
	case strings.Contains(message, "runtime") ||
		strings.Contains(message, "health") ||
		strings.Contains(message, "webui"):
		return "本地服务未能完成启动，现有可用版本已保留。请重新打开 e-Mate。"
	case strings.Contains(message, "root") ||
		strings.Contains(message, "workspace") ||
		strings.Contains(message, "directory") ||
		strings.Contains(message, "atomic"):
		return "本机存储空间或目录权限不足。释放空间后重新运行安装命令。"
	default:
		return "安装没有完成，现有版本未被修改。请重新运行安装命令。"
	}
}
