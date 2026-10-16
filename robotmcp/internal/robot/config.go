package robot

import (
	"fmt"
	"math"
	"os"
	"regexp"

	"gopkg.in/yaml.v3"
)

// Place — именованное место на карте.
type Place struct {
	ID      string    `yaml:"id" json:"-"`
	XY      []float64 `yaml:"xy" json:"xy"`
	YawDeg  *float64  `yaml:"yaw_deg,omitempty" json:"yaw_deg,omitempty"` // курс в цели (нет — 0, как в прототипе)
	RU      string    `yaml:"ru" json:"-"`
	RULoc   string    `yaml:"ru_loc" json:"-"`
	Alias   string    `yaml:"alias,omitempty" json:"-"` // имя в прототипе и замерах 07.10
	Charger bool      `yaml:"charger,omitempty" json:"charger,omitempty"`
	Role    string    `yaml:"role,omitempty" json:"-"`
}

// X, Y — координаты места.
func (p Place) X() float64 { return p.XY[0] }
func (p Place) Y() float64 { return p.XY[1] }

// Travel — модель времени участка t ≈ A + B·L, L ≈ PathFactor·(расстояние по прямой)
// (robot_scenario.md §3: 11,1 + 2,0·L по 29 участкам 07.10; путь Nav2 в среднем ≈1,2 прямой).
type Travel struct {
	A          float64 `yaml:"a_s"`
	B          float64 `yaml:"b_s_per_m"`
	PathFactor float64 `yaml:"path_factor"`
}

// Topics — имена тем и действий ROS (конфигурация адаптера, а не каталог LLM).
type Topics struct {
	CmdVel    string `yaml:"cmd_vel"`
	AMCL      string `yaml:"amcl_pose"`
	Scan      string `yaml:"scan"`
	Camera    string `yaml:"camera"`
	Battery   string `yaml:"battery"`
	Nav       string `yaml:"navigate_to_pose"`
	WorldCtrl string `yaml:"world_control"`
	Clock     string `yaml:"clock"`
}

// Config — конфигурация мира и робота (configs/turtlebot3_world.yaml).
type Config struct {
	World        string  `yaml:"world"`
	Frame        string  `yaml:"frame"`
	Model        string  `yaml:"model"`
	GoalTolM     float64 `yaml:"goal_tol_m"`
	MaxVMps      float64 `yaml:"max_v_mps"`
	PlaceRadiusM float64 `yaml:"place_radius_m"`
	CameraHFOV   float64 `yaml:"camera_hfov_deg"`
	Travel       Travel  `yaml:"travel"`
	Topics       Topics  `yaml:"topics"`
	Places       []Place `yaml:"places"`

	byID map[string]int
}

var idRe = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)

// LoadConfig читает YAML (или JSON) и проверяет его.
func LoadConfig(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParseConfig(b)
}

// ParseConfig разбирает конфигурацию и заполняет умолчания.
func ParseConfig(b []byte) (*Config, error) {
	var c Config
	if err := yaml.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("config: %w", err)
	}
	def := func(v *float64, d float64) {
		if *v == 0 {
			*v = d
		}
	}
	defs := func(v *string, d string) {
		if *v == "" {
			*v = d
		}
	}
	defs(&c.World, "default")
	defs(&c.Frame, "map")
	def(&c.GoalTolM, 0.25)
	def(&c.MaxVMps, 0.26)
	def(&c.PlaceRadiusM, 0.45)
	def(&c.CameraHFOV, 62.2)
	def(&c.Travel.A, 11.1)
	def(&c.Travel.B, 2.0)
	def(&c.Travel.PathFactor, 1.2)
	defs(&c.Topics.CmdVel, "/cmd_vel")
	defs(&c.Topics.AMCL, "/amcl_pose")
	defs(&c.Topics.Scan, "/scan")
	defs(&c.Topics.Camera, "/camera/image_raw/compressed")
	defs(&c.Topics.Battery, "/battery_state")
	defs(&c.Topics.Nav, "/navigate_to_pose")
	defs(&c.Topics.WorldCtrl, "/world/default/control")
	defs(&c.Topics.Clock, "/clock")
	if len(c.Places) == 0 {
		return nil, fmt.Errorf("config: no places")
	}
	c.byID = map[string]int{}
	for i, p := range c.Places {
		if !idRe.MatchString(p.ID) || p.ID == Between || p.ID == Unknown {
			return nil, fmt.Errorf("config: bad place id %q (want [a-z][a-z0-9_]*, not %q or %q)", p.ID, Between, Unknown)
		}
		if _, dup := c.byID[p.ID]; dup {
			return nil, fmt.Errorf("config: duplicate place %q", p.ID)
		}
		if len(p.XY) != 2 || math.IsNaN(p.XY[0]) || math.IsNaN(p.XY[1]) {
			return nil, fmt.Errorf("config: place %q: xy must be [x, y]", p.ID)
		}
		c.byID[p.ID] = i
	}
	for i := range c.Places {
		for j := i + 1; j < len(c.Places); j++ {
			if d := dist(c.Places[i].X(), c.Places[i].Y(), c.Places[j].X(), c.Places[j].Y()); d < 2*c.PlaceRadiusM {
				return nil, fmt.Errorf("config: places %q and %q are %.2f m apart (< 2·place_radius_m %.2f)",
					c.Places[i].ID, c.Places[j].ID, d, 2*c.PlaceRadiusM)
			}
		}
	}
	return &c, nil
}

// Place — место по id.
func (c *Config) Place(id string) (Place, bool) {
	i, ok := c.byID[id]
	if !ok {
		return Place{}, false
	}
	return c.Places[i], true
}

// IDs — id мест в порядке конфигурации.
func (c *Config) IDs() []string {
	out := make([]string, len(c.Places))
	for i, p := range c.Places {
		out[i] = p.ID
	}
	return out
}

// Nearest — ближайшее место и расстояние до него.
func (c *Config) Nearest(x, y float64) (Place, float64) {
	best, bd := c.Places[0], math.Inf(1)
	for _, p := range c.Places {
		if d := dist(x, y, p.X(), p.Y()); d < bd {
			best, bd = p, d
		}
	}
	return best, bd
}

// ETA — оценка времени поездки по прямой d м, с.
func (c *Config) ETA(d float64) float64 {
	if d < c.GoalTolM {
		return 0
	}
	return c.Travel.A + c.Travel.B*c.Travel.PathFactor*d
}

func dist(x1, y1, x2, y2 float64) float64 { return math.Hypot(x1-x2, y1-y2) }
