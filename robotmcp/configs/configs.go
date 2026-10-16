// Package configs — конфигурации мира, встроенные в бинарник (плата получает один файл).
package configs

import _ "embed"

// TurtleBot3World — места и параметры turtlebot3_world (конфигурация по умолчанию).
//
//go:embed turtlebot3_world.yaml
var TurtleBot3World []byte
