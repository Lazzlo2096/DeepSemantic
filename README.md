# DeepSemantic

## Развёртывание на Ubuntu

Выполни на сервере от имени `root`.

```sh
git clone --depth 1 https://github.com/Lazzlo2096/DeepSemantic.git
cd DeepSemantic

chmod +x ./bootstrap.sh
./bootstrap.sh
```

После установки запускай и останавливай проект из каталога `DeepSemantic`.

```sh
just up-server # снова запустить проект
just down      # остановить проект
```
