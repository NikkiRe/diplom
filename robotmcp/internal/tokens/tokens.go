// Package tokens — оценка числа токенов o200k_base (GPT-4o/4.1/5) без словаря BPE.
//
// Текст режется на куски тем же правилом предварительной разбивки, что у o200k (слова с необязательным
// префиксом-знаком и делением по регистру, числа по 1–3 цифры, серии знаков, пробелы), затем кусок
// оценивается по длине: короткие латинские слова и числа — один токен, длинные — несколько, кириллица
// дробится мельче. Константы подобраны по эталону testdata/tokens_golden.json (точный o200k на ответах
// R-compact и ros-mcp из smoke 07.10, каталогах и русских репликах); ошибка на эталоне проверяется тестом.
// Это учёт «на лету» в адаптере (журнал, /control/state); числа для ПЗ считаются точно, tools/analyze.py.
package tokens

import (
	"unicode"
	"unicode/utf8"
)

// Константы оценки куска (подобраны по эталону, см. tokens_test.go); var — чтобы тест мог перебрать.
var (
	latinPerToken    = 8.0 // латинских букв на токен в длинных словах
	cyrillicPerToken = 3.4 // кириллических букв на токен
	punctPerToken    = 3.0 // знаков на токен в длинных сериях знаков
)

// Count — оценка числа токенов текста.
func Count(s string) int {
	n := 0
	for i := 0; i < len(s); {
		j, cost := piece(s, i)
		n += cost
		i = j
	}
	return n
}

func isLetter(r rune) bool { return unicode.IsLetter(r) || unicode.Is(unicode.M, r) }
func isDigit(r rune) bool  { return unicode.IsNumber(r) }
func isNL(r rune) bool     { return r == '\r' || r == '\n' }

func at(s string, i int) (rune, int) {
	if i >= len(s) {
		return -1, 0
	}
	return utf8.DecodeRuneInString(s[i:])
}

// piece возвращает конец куска, начинающегося в i, и его оценку в токенах.
func piece(s string, i int) (int, int) {
	r, w := at(s, i)
	// слово: [^\r\n\p{L}\p{N}]? + буквы
	start := i
	if !isLetter(r) && !isDigit(r) && !isNL(r) {
		if r2, _ := at(s, i+w); r2 >= 0 && isLetter(r2) {
			i += w
			r, w = at(s, i)
		}
	}
	if isLetter(r) {
		j := letters(s, i)
		return j, wordCost(s[i:j])
	}
	i = start
	r, w = at(s, i)
	switch {
	case isDigit(r): // \p{N}{1,3}
		j := i
		for k := 0; k < 3; k++ {
			r2, w2 := at(s, j)
			if r2 < 0 || !isDigit(r2) {
				break
			}
			j += w2
		}
		return j, 1
	case unicode.IsSpace(r):
		j := i
		nl := false
		for {
			r2, w2 := at(s, j)
			if r2 < 0 || !unicode.IsSpace(r2) {
				break
			}
			nl = nl || isNL(r2)
			j += w2
		}
		// \s+(?!\S): последний пробел перед непробельным куском уходит в следующий кусок
		if !nl && j < len(s) && j-i > 1 {
			return j - 1, 1
		}
		if !nl && j < len(s) && s[i] == ' ' {
			// одиночный пробел: прилипает к следующему слову/знакам
			nxt, cost := piece(s, j)
			if r3, _ := at(s, j); isLetter(r3) || (!isDigit(r3) && !unicode.IsSpace(r3)) {
				return nxt, cost
			}
		}
		return j, 1
	default: // ` ?[^\s\p{L}\p{N}]+[\r\n/]*`
		j := i
		n := 0
		for {
			r2, w2 := at(s, j)
			if r2 < 0 || unicode.IsSpace(r2) || isLetter(r2) || isDigit(r2) {
				break
			}
			j += w2
			n++
		}
		for {
			r2, w2 := at(s, j)
			if r2 != '\r' && r2 != '\n' && r2 != '/' {
				break
			}
			j += w2
		}
		if j == i { // не должно случаться; защита от зацикливания
			return i + w, 1
		}
		return j, 1 + int(float64(n-1)/punctPerToken)
	}
}

// letters — конец серии букв по правилу регистра o200k: Upper* Lower+ | Upper+ Lower*.
func letters(s string, i int) int {
	j := i
	for {
		r, w := at(s, j)
		if r < 0 || !isLetter(r) || unicode.IsLower(r) {
			break
		}
		j += w
	}
	for {
		r, w := at(s, j)
		if r < 0 || !isLetter(r) || unicode.IsUpper(r) {
			break
		}
		j += w
	}
	if j == i {
		_, w := at(s, i)
		j = i + w
	}
	return j
}

func wordCost(w string) int {
	n, cyr := 0, 0
	for _, r := range w {
		n++
		if unicode.Is(unicode.Cyrillic, r) {
			cyr++
		}
	}
	per := latinPerToken
	if cyr*2 > n {
		per = cyrillicPerToken
	}
	return 1 + int(float64(n-1)/per)
}
