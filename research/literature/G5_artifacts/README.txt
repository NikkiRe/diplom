G5 artifacts (2026-10-05)

fmcheck.swift — минимальная проверка доступности on-device модели Apple Foundation Models.
Сборка и запуск:
  swiftc -O fmcheck.swift -o fmcheck && ./fmcheck
Окружение: macOS 27.0.1 (26A434), Command Line Tools SDK 26.5, Apple Swift 6.3.2.
Результат на M4 Pro 2026-10-05:
  availability: unavailable(FoundationModels.SystemLanguageModel.Availability.UnavailableReason.modelNotReady)
Интерпретация: устройство поддерживается (не deviceNotEligible), но модель не готова
(Apple Intelligence не включён или модель не загружена). Нужно включить Apple Intelligence
в настройках и повторить.

Проверка SDK: в CLT SDK 26.5 протокол Tool есть (FoundationModels.swiftinterface:1184),
а LanguageModelExecutor (документирован как macOS 27.0+) отсутствует.
Для собственного LanguageModelExecutor нужен SDK macOS 27 (Xcode 27).
